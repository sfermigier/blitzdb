import time
import copy
import sqlalchemy
import pprint

from blitzdb.queryset import QuerySet as BaseQuerySet
from functools import wraps
from sqlalchemy.sql import select,func,expression,delete,distinct,and_,union,intersect
from sqlalchemy.sql.expression import join,asc,desc,outerjoin
from ..file.serializers import JsonSerializer

class QuerySet(BaseQuerySet):

    def __init__(self, backend, table, cls,
                 condition = None,
                 select = None,
                 intersects = None,
                 raw = False,
                 joins = None,
                 include = None,
                 extra_fields = None,
                 group_bys = None,
                 objects = None,
                 havings = None,
                 limit = None,
                 offset = None
                 ):
        super(QuerySet,self).__init__(backend = backend,cls = cls)

        self.joins = joins
        self.backend = backend
        self.condition = condition
        self.select = select
        self.include = include
        self.objects = objects
        self.havings = havings
        self.extra_fields = extra_fields
        self.group_bys = group_bys
        self.cls = cls
        self._limit = limit
        self._offset = offset
        self.table = table
        self._it = None
        self._raw = raw
        self.count = None
        self.order_bys = None
        self.result = None
        self.intersects = intersects
        self.objects = None
        self.pop_objects = None

    def limit(self,limit):
        self._limit = limit
        return self

    def offset(self,offset):
        self._offset = offset
        return self

    def deserialize(self, data):
        return self.backend.create_instance(self.cls, data)

    def sort(self, keys,direction = None):
        #we sort by a single argument
        if direction:
            keys = ((keys,direction),)
        order_bys = []
        for key,direction in keys:
            if direction > 0:
                direction = asc
            else:
                direction = desc
            try:
                column = self.backend.get_column_for_key(self.cls,key)
            except KeyError:
                raise AttributeError("Attempting to sort results by a non-indexed field %s" % key)
            order_bys.append(direction(column))
        self.order_bys = order_bys
        self.objects = None
        return self

    def next(self):
        if self._it is None:
            self._it = iter(self)
        return self._it.next()

    __next__ = next

    def __iter__(self):
        if self.objects is None:
            self.get_objects()
        for obj in self.objects:
            yield self.deserialize(obj)
        raise StopIteration

    def __contains__(self, obj):
        pks = self.distinct_pks()
        if isinstance(obj, list) or isinstance(obj, tuple):
            obj_list = obj
        else:
            obj_list = [obj]
        for obj in obj_list:
            if obj.pk not in pks:
                return False
        return True

    def get_objects(self):
        s = self.get_select()

        #We create a CTE, which will allow us to join the required includes.
        s_cte = s.cte("results")
        rows = []
        joins = []
        keymap = {}

        """
        For each include field, we add an OUTER JOIN and either request the whole table
        (if not 'fields' variable is present), or only a subset of the fields.
        """

        def join_table(collection,table,key,params,path = None):
            if path is None:
                path = []
            if 'relationship_table' in params['relation']:
                join_many_to_many(collection,table,key,params,path)
            else:
                join_foreign_key(collection,table,key,params,path)

        def update_keymap(path,field,label):
            cd = keymap
            for path_element in path:
                if not path_element in cd:
                    cd[path_element] = {}
                cd = cd[path_element]
            cd[field] = label

        def process_fields_and_subkeys(related_collection,related_table,params,path):

            if 'fields' in params and params['fields']:
                if not 'pk' in params['fields']:#we always include the primary key
                    pk_label = "_".join(path)+'_pk'
                    rows.append(related_table.c['pk'].label(pk_label))
                    update_keymap(path,'pk',pk_label)
                for field in params['fields']:
                    column_name = self.backend.get_column_for_key(related_collection,field)
                    column_label = '_'.join(path)+'_%s' % column_name
                    rows.append(related_table.c[field].label(column_label))
                    update_keymap(path,column_name,column_label)
            else:
                index_fields = self.backend.get_table_columns(related_collection)
                for field,field_params in index_fields.items():
                    column_name = field_params['column']
                    column_label = '_'.join(path)+'_%s' % column_name
                    rows.append(related_table.c[column_name].label(column_label))
                    update_keymap(path,column_name,column_label)
            for subkey,subparams in params['joins'].items():
                join_table(params['collection'],related_table,subkey,subparams,path = path)

        def join_foreign_key(collection,table,key,params,path):
            related_table = params['table'].alias()
            related_collection = params['relation']['collection']
            condition = table.c[params['relation']['column']] == related_table.c.pk
            joins.append((related_table,condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+\
                                        [params['relation']['column']])
            update_keymap(path+[key],'__foreign_key',True)

        def join_many_to_many(collection,table,key,params,path):
            relationship_table = params['relation']['relationship_table'].alias()
            related_collection = params['relation']['collection']
            related_table = self.backend.get_collection_table(related_collection).alias()
            left_condition = relationship_table.c['pk_%s' % collection] == table.c.pk
            right_condition = relationship_table.c['pk_%s' % related_collection] == related_table.c.pk
            joins.append((relationship_table,left_condition))
            joins.append((related_table,right_condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+[key])
            update_keymap(path+[key],'__many_to_many',True)

        if self.include:
            include_joins = self.backend.get_include_joins(self.cls,self.include)

            if include_joins['fields']:
                if not 'pk' in include_joins['fields']:#we always include the primary key
                    rows.append(s_cte.c['pk'])
                    keymap['pk'] = 'pk'
                for field in include_joins['fields']:
                    rows.append(s_cte.c[field])
                    keymap[field] = field
            else:
                rows.append(s_cte)
                for column in s_cte.columns:
                    keymap[column.name] = column.name

            for key,params in include_joins['joins'].items():
                join_table(include_joins['collection'],s_cte,key,params)
        else:
            rows.append(s_cte)
            for column in s_cte.columns:
                keymap[column.name] = column.name

        if joins:
            for j in joins:
                s_cte = s_cte.outerjoin(*j)

        with self.backend.transaction(use_auto = False):
            try:
                result = self.backend.connection.execute(select(rows).select_from(s_cte))
                if result.returns_rows:
                    objects = list(result.fetchall())
                else:
                    objects = []
            except sqlalchemy.exc.ResourceClosedError:
                objects = None
                raise

        pprint.pprint(keymap)

        def unpack_many_to_many(objects,keymap,pk_key,pk_value):
            """
            We unpack a many-to-many relation:

            * We unpack a single object from the first row in the result set.
            * We check if the primary key of the nxt row (if it exists) is the same
            * If it is, we pop a row from the set and repeat
            * If not, we return the results 
              (the unpack_single_object will pop the object in that case)
            """
            objs = []
            while True:
                objs.append(unpack_single_object(objects,keymap,nested = True))
                if len(objects) > 1 and objects[1][pk_key] == pk_value:
                    objects.pop(0)
                else:
                    break
            return objs

        def unpack_single_object(objects,keymap,nested = False):
            """
            We unpack a single object from the result set:

            * We iterate over the key,value pairs in the keymap.
            * if the given value is a many-to-many relation, we call unpack_amny_to_many
            * if the given value is a foreign-key relation, we recursively call unpack_single_object
            * otherwise we just update the result dictionary
            * if unpack_single_object was called from the top-level, we pop a result from the set
            * we return the resulting dict
            """
            obj = objects[0]
            d = {}
            for key,value in keymap.items():
                if key in ('__foreign_key','__many_to_many'):
                    continue
                if isinstance(value,dict):
                    if '__many_to_many' in value:
                        d[key] = unpack_many_to_many(objects,value,keymap['pk'],obj[keymap['pk']])
                    else:
                        d[key] = unpack_single_object(objects,value,nested = True)
                else:
                    d[key] = obj[value]
            if not nested:
                objects.pop(0)
            return d

        #we "fold" the objects back into one list structure
        self.objects = []

        while objects:
            self.objects.append(unpack_single_object(objects,keymap))

        pprint.pprint(self.objects)

        self.pop_objects = self.objects[:]

    def as_list(self):
        if self.objects is None:
            self.get_objects()
        return [self.deserialize(obj) for obj in self.objects]

    def __getitem__(self,key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step
            if step != None:
                raise IndexError("SQL backend dos not support steps in slices")
            if key.start == None:
                start = 0
            if key.stop == None:
                stop = len(self)
            if start < 0:
                start = len(self) + start
            if stop < 0:
                stop = len(self) + stop
            qs = copy.copy(self)
            if start:
                qs.offset(start)
            qs.limit(stop-start)
            qs.objects = None
            qs.count = None
            return qs
        if self.objects is None:
            self.get_objects()
        return self.deserialize(self.objects[key])

    def pop(self,i = 0):
        if self.objects is None:
            self.get_objects()
        if self.pop_objects:
            return self.deserialize(self.pop_objects.pop())
        raise IndexError("pop from empty list")

    def filter(self,*args,**kwargs):
        qs = self.backend.filter(self.cls,*args,**kwargs)
        return self.intersect(qs)

    def intersect(self,qs):
        new_qs = QuerySet(self.backend,self.table,self.cls,select = intersect(self.get_select(),qs.get_select()))
        return new_qs

    def delete(self):
        with self.backend.transaction(use_auto = False):
            delete_stmt = self.table.delete().where(self.table.c.pk.in_(self.get_select(fields = [self.table.c.pk])))
            self.backend.connection.execute(delete_stmt)

    def get_fields(self):
        return [self.table]

    def get_select(self,fields = None):
        if self.select is not None:
            return self.select
        if fields is None:
            fields = self.get_fields()
        if self.extra_fields:
            fields.extend(self.extra_fields)
        s = select(fields)
        if self.joins:
            full_join = None
            for j in self.joins:
                if full_join is not None:
                    full_join = full_join.join(*j)
                else:
                    full_join = outerjoin(self.table,*j)
            s = s.select_from(full_join)

        if self.condition is not None:
            s = s.where(self.condition)
        if self.group_bys:
            s = s.group_by(*self.group_bys)
        if self.havings:
            for having in self.havings:
                s = s.having(having)
        if self.order_bys:
            s = s.order_by(*self.order_bys)
        if self._offset:
            s = s.offset(self._offset)
        if self._limit:
            s = s.limit(self._limit)
        return s

    def __len__(self):
        if self.count is None:
            with self.backend.transaction(use_auto = False):
                s = select([func.count()]).select_from(self.get_select(fields = [self.table.c.pk]).alias('count_select'))
                result = self.backend.connection.execute(s)
                self.count = result.first()[0]
                result.close()
        return self.count

    def distinct_pks(self):
        with self.backend.transaction(use_auto = False):
            s = self.get_select([self.table.c.pk]).distinct(self.table.c.pk)
            result = self.backend.connection.execute(s)
            return set([r[0] for r in result.fetchall()])
        
    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __eq__(self, other):
        if isinstance(other, QuerySet): 
            if self.cls == other.cls and len(self) == len(other) \
              and self.distinct_pks() == other.distinct_pks():
                return True
        elif isinstance(other, list):
            if len(other) != len(self.keys):
                return False
            objs = list(self)
            if other == objs:
                return True
        return False

