from __future__ import absolute_import, print_function, unicode_literals

import abc
import copy
import inspect
import logging

import six

from blitzdb.document import Document, document_classes

logger = logging.getLogger(__name__)


class DoNotSerialize(BaseException):
    """
    If an encoder throws this exception, the object in question will not get serialized.
    """


class NotInTransaction(BaseException):
    """
    Gets raised if a function that must only be used inside a database transaction
    gets called outside a transaction.
    """


class InTransaction(BaseException):
    """
    Gets raised if a function that must only be used outside a database transaction
    gets called inside a transaction.
    """


class ComplexEncoder(object):
    @classmethod
    def encode(cls, obj, path):
        if isinstance(obj, complex):
            return {"_type": "complex", "r": obj.real, "i": obj.imag}

        return obj

    @classmethod
    def decode(cls, obj):
        if isinstance(obj, dict) and obj.get("_type") == "complex":
            return 1j * obj["i"] + obj["r"]

        return obj


class ComplexQueryEncoder(object):
    @classmethod
    def encode(cls, obj, path):
        if isinstance(obj, complex):
            raise ValueError(
                "Currently complex values are not supported in queries! "
                "Please write your queries using the imaginary and real values instead."
            )

        return obj


class Backend(object):

    """
    Abstract base class for all backend implementations. Provides operations for querying the database,
    as well as for storing, updating and deleting documents.

    :param autodiscover_classes: If set to `True`, document classes will be discovered automatically,
                                 using a global list of all classes generated by the Document metaclass.

    *The `Meta` attribute*

    As with `blitzdb.document.Document`, the `Meta` attribute can be used to define certain class-wide
    settings and properties. Redefine it in your backend implementation to change the default values.

    """

    class Meta(object):
        pass

    __metaclass__ = abc.ABCMeta

    standard_encoders = [ComplexEncoder]
    query_encoders = [ComplexQueryEncoder]

    def __init__(
        self,
        autodiscover_classes=True,
        autoload_embedded=True,
        allow_documents_in_query=True,
    ):
        self.classes = {}
        self.deprecated_classes = {}
        self.collections = {}
        self._autoload_embedded = autoload_embedded
        self._allow_documents_in_query = allow_documents_in_query
        if autodiscover_classes:
            self.autodiscover_classes()

    def autodiscover_classes(self):
        """
        Registers all document classes that have been defined in the code so far. The discovery mechanism
        works by reading the value of `blitzdb.document.document_classes`, which is updated by the meta-class
        of the :py:class:`blitzdb.document.Document` class upon creation of a new subclass.
        """
        for document_class in document_classes:
            self.register(document_class)

    def unregister(self, cls):

        if cls in self.classes:
            del self.collections[self.classes[cls]["collection"]]
            del self.classes[cls]

    def register(self, cls, parameters=None, overwrite=False):
        """
        Explicitly register a new document class for use in the backend.

        :param cls:        A reference to the class to be defined
        :param parameters: A dictionary of parameters. Currently, only the `collection` parameter is used
                           to specify the collection in which to store the documents of the given class.

        .. admonition:: Registering classes

            If possible, always use `autodiscover_classes = True` or register your document classes beforehand
            using the `register` function, since this ensures that related documents can be initialized
            appropriately. For example, suppose you have a document class `Author` that contains a list of
            references to documents of class `Book`. If you retrieve an instance of an `Author` object from
            the database without having registered the `Book` class, the references to that class will not get
            parsed properly and will just show up as dictionaries containing a primary key and a collection name.

            Also, when :py:meth:`blitzdb.backends.base.Backend.autoregister` is used to register a class,
            you can't pass in any parameters to customize e.g. the collection name for that class
            (you can of course do this throught the `Meta` attribute of the class)

        ## Inheritance

        If `register` encounters a document class with a collection name that overlaps with the
        collection name of an already registered document class, it checks if the new class is a
        subclass of the one that is already register. If yes, it associates the new class to the
        collection name. Otherwise, it leaves the collection associated to the already
        registered class.
        """

        if cls in self.deprecated_classes and not overwrite:
            return False

        if parameters is None:
            parameters = {}
        if "collection" in parameters:
            collection_name = parameters["collection"]
        elif hasattr(cls.Meta, "collection"):
            collection_name = cls.Meta.collection
        else:
            collection_name = cls.__name__.lower()

        delete_list = []

        def register_class(collection_name, cls):
            self.collections[collection_name] = cls
            self.classes[cls] = parameters.copy()
            self.classes[cls]["collection"] = collection_name

        if collection_name in self.collections:
            old_cls = self.collections[collection_name]
            if (issubclass(cls, old_cls) and not (cls is old_cls)) or overwrite:
                logger.warning(
                    "Replacing class %s with %s for collection %s"
                    % (old_cls, cls, collection_name)
                )
                self.deprecated_classes[old_cls] = self.classes[old_cls]
                del self.classes[old_cls]
                register_class(collection_name, cls)
                return True

        else:
            logger.debug(
                "Registering class %s under collection %s" % (cls, collection_name)
            )
            register_class(collection_name, cls)
            return True

        return False

    def get_meta_attributes(self, cls):
        def get_user_attributes(cls):
            if six.PY2:
                boring = dir(type(b"dummy", (object,), {}))
            else:
                boring = dir(type("dummy", (object,), {}))
            return dict(
                [item for item in inspect.getmembers(cls) if item[0] not in boring]
            )

        if hasattr(cls, "Meta"):
            params = get_user_attributes(cls.Meta)
        else:
            params = {}

        return params

    def autoregister(self, cls):
        """
        Autoregister a class that is encountered for the first time.

        :param cls: The class that should be registered.
        """

        params = self.get_meta_attributes(cls)
        return self.register(cls, params)

    def serialize(
        self,
        obj,
        convert_keys_to_str=False,
        embed_level=0,
        encoders=None,
        autosave=True,
        for_query=False,
        path=None,
    ):
        """
        Serializes a given object, i.e. converts it to a representation that can be stored in the database.
        This usually involves replacing all `Document` instances by database references to them.

        :param obj: The object to serialize.
        :param convert_keys_to_str: If `True`, converts all dictionary keys to string (this is e.g. required for the MongoDB backend)
        :param embed_level: If `embed_level > 0`, instances of `Document` classes will be embedded instead of referenced.
                            The value of the parameter will get decremented by 1 when calling `serialize` on child objects.
        :param autosave: Whether to automatically save embedded objects without a primary key to the database.
        :param for_query: If true, only the `pk` and `__collection__` attributes will be included in document references.

        :returns: The serialized object.
        """

        if path is None:
            path = []

        def get_value(obj, key):
            key_fragments = key.split(".")
            current_dict = obj
            for key_fragment in key_fragments:
                current_dict = current_dict[key_fragment]
            return current_dict

        serialize_with_opts = lambda value, *args, **kwargs: self.serialize(
            value,
            *args,
            encoders=encoders,
            convert_keys_to_str=convert_keys_to_str,
            autosave=autosave,
            for_query=for_query,
            **kwargs
        )

        if encoders is None:
            encoders = []

        for encoder in self.standard_encoders + encoders:
            obj = encoder.encode(obj, path=path)

        def encode_as_str(obj):
            if six.PY3:
                return str(obj)

            else:
                if isinstance(obj, unicode):
                    return obj

                elif isinstance(obj, str):
                    return unicode(obj)

                else:
                    return unicode(str(obj), errors="replace")

        if isinstance(obj, dict):
            output_obj = {}
            for key, value in obj.items():
                new_path = path[:] + [key]
                try:
                    output_obj[
                        encode_as_str(key) if convert_keys_to_str else key
                    ] = serialize_with_opts(
                        value, embed_level=embed_level, path=new_path
                    )
                except DoNotSerialize:
                    pass
        elif isinstance(obj, six.string_types):
            output_obj = encode_as_str(obj)
        elif isinstance(obj, (list, tuple)):
            try:
                output_obj = [
                    serialize_with_opts(x, embed_level=embed_level, path=path[:] + [i])
                    for i, x in enumerate(obj)
                ]
            except DoNotSerialize:
                pass
        elif isinstance(obj, Document):
            collection = self.get_collection_for_obj(obj)
            if embed_level > 0:
                try:
                    output_obj = self.serialize(obj, embed_level=embed_level - 1)
                except obj.DoesNotExist:  # cannot load object, ignoring...
                    output_obj = self.serialize(
                        obj.lazy_attributes, embed_level=embed_level - 1
                    )
                except DoNotSerialize:
                    pass
            elif obj.embed:
                output_obj = self.serialize(obj)
            else:
                if obj.pk == None and autosave:
                    obj.save(self)

                if obj._lazy:
                    # We make sure that all attributes that are already present get included in the reference
                    output_obj = {}
                    if obj.get_pk_name() in output_obj:
                        del output_obj[obj.get_pk_name()]
                    output_obj["pk"] = obj.pk
                    output_obj["__collection__"] = self.classes[obj.__class__][
                        "collection"
                    ]
                else:
                    if for_query and not self._allow_documents_in_query:
                        raise ValueError("Documents are not allowed in queries!")

                    if for_query:
                        output_obj = {
                            "$elemMatch": {
                                "pk": obj.pk,
                                "__collection__": self.classes[obj.__class__][
                                    "collection"
                                ],
                            }
                        }
                    else:
                        ref = "%s:%s" % (
                            self.classes[obj.__class__]["collection"],
                            str(obj.pk),
                        )
                        output_obj = {
                            "__ref__": ref,
                            "pk": obj.pk,
                            "__collection__": self.classes[obj.__class__]["collection"],
                        }

                if (
                    hasattr(obj, "Meta")
                    and hasattr(obj.Meta, "dbref_includes")
                    and obj.Meta.dbref_includes
                ):
                    for include_key in obj.Meta.dbref_includes:
                        try:
                            value = get_value(obj, include_key)
                            output_obj[include_key.replace(".", "_")] = value
                        except KeyError:
                            continue

        else:
            output_obj = obj
        return output_obj

    def deserialize(self, obj, encoders=None, embedded=False, create_instance=True):
        """
        Deserializes a given object, i.e. converts references to other (known) `Document` objects by lazy instances of the
        corresponding class. This allows the automatic fetching of related documents from the database as required.

        :param obj: The object to be deserialized.

        :returns: The deserialized object.
        """

        if not encoders:
            encoders = []

        for encoder in encoders + self.standard_encoders:
            obj = encoder.decode(obj)

        if isinstance(obj, dict):
            if (
                create_instance
                and "__collection__" in obj
                and obj["__collection__"] in self.collections
                and "pk" in obj
            ):
                # for backwards compatibility
                attributes = copy.deepcopy(obj)
                del attributes["__collection__"]
                if "__ref__" in attributes:
                    del attributes["__ref__"]
                if "__lazy__" in attributes:
                    lazy = attributes["__lazy__"]
                    del attributes["__lazy__"]
                else:
                    lazy = True
                output_obj = self.create_instance(
                    obj["__collection__"], attributes, lazy=lazy
                )
            else:
                output_obj = {}
                for key, value in obj.items():
                    output_obj[key] = self.deserialize(value, encoders=encoders)
        elif isinstance(obj, (list, tuple)):
            output_obj = list(map(lambda x: self.deserialize(x), obj))
        else:
            output_obj = obj

        return output_obj

    def create_instance(
        self,
        collection_or_class,
        attributes,
        lazy=False,
        call_hook=True,
        deserialize=True,
        db_loader=None,
    ):
        """
        Creates an instance of a `Document` class corresponding to the given collection name or class.

        :param collection_or_class: The name of the collection or a reference to the class for which to create an instance.
        :param attributes: The attributes of the instance to be created
        :param lazy: Whether to create a `lazy` object or not.

        :returns: An instance of the requested Document class with the given attributes.
        """
        creation_args = {
            "backend": self,
            "autoload": self._autoload_embedded,
            "lazy": lazy,
            "db_loader": db_loader,
        }

        if collection_or_class in self.classes:
            cls = collection_or_class
        elif collection_or_class in self.collections:
            cls = self.collections[collection_or_class]
        else:
            raise AttributeError(
                "Unknown collection or class: %s!" % str(collection_or_class)
            )

        # we deserialize the attributes that we receive
        if deserialize:
            deserialized_attributes = self.deserialize(
                attributes, create_instance=False
            )
        else:
            deserialized_attributes = attributes

        if "constructor" in self.classes[cls]:
            obj = self.classes[cls]["constructor"](
                deserialized_attributes, **creation_args
            )
        else:
            obj = cls(deserialized_attributes, **creation_args)

        if call_hook:
            self.call_hook("after_load", obj)

        return obj

    @property
    @abc.abstractmethod
    def current_transaction(self):
        pass

    def transaction(self, implicit=False):
        """
        This returns a context guard which will automatically open and close a transaction
        """

        class TransactionManager(object):
            def __init__(self, backend, implicit=False):
                self.backend = backend
                self.implicit = implicit

            def __enter__(self):
                self.within_transaction = bool(self.backend.current_transaction)
                self.transaction = self.backend.begin()

            def __exit__(self, exc_type, exc_value, traceback_obj):
                if exc_type:
                    self.backend.rollback(self.transaction)
                    return False

                else:
                    # if the transaction has been created implicitly and we are not within
                    # another transaction, we leave it open (the user needs to call commit manually)
                    # if self.implicit and not self.within_transaction:
                    #    return
                    self.backend.commit(self.transaction)

        return TransactionManager(self, implicit=implicit)

    def get_collection_for_obj(self, obj):
        """
        Returns the collection name for a given object, based on the class of the object.

        :param obj: The object for which to return the collection name.

        :returns: The collection name for the given object.
        """
        return self.get_collection_for_cls(obj.__class__)

    def get_collection_for_cls(self, cls):
        """
        Returns the collection name for a given document class.

        :param cls: The document class for which to return the collection name.

        :returns: The collection name for the given class.
        """
        if cls not in self.classes:
            if (
                issubclass(cls, Document)
                and cls not in self.classes
                and cls not in self.deprecated_classes
            ):
                self.autoregister(cls)
            else:
                raise AttributeError("Unknown object type: %s" % cls.__name__)

        collection = self.classes[cls]["collection"]
        return collection

    def get_collection_for_cls_name(self, cls_name):
        """
        Returns the collection name for a given document class.

        :param cls: The document class for which to return the collection name.

        :returns: The collection name for the given class.
        """
        for cls in self.classes:
            if cls.__name__ == cls_name:
                return self.classes[cls]["collection"]

        raise AttributeError("Unknown class name: %s" % cls_name)

    def get_cls_for_collection(self, collection):
        """
        Return the class for a given collection name.

        :param collection: The name of the collection for which to return the class.

        :returns: A reference to the class for the given collection name.
        """
        for cls, params in self.classes.items():
            if params["collection"] == collection:
                return cls

        raise AttributeError("Unknown collection: %s" % collection)

    def call_hook(self, name, obj, *args, **kwargs):
        try:
            hook = obj.get_lazy_attribute(name)
            return hook(*args, **kwargs)

        except AttributeError:
            pass

    @abc.abstractmethod
    def save(self, obj, cache=None):
        """
        Abstract method to save a `Document` instance to the database.

        :param obj: The object to be stored in the database.
        :param cache: Whether to performed a cached save operation (not supported by all backends).
        """

    @abc.abstractmethod
    def get(self, cls, properties):
        """
        Abstract method to retrieve a single object from the database according to a list of properties.

        :param cls: The class for which to return an object.
        :param properties: The properties of the object to be returned

        :returns: An instance of the requested object.

        .. admonition:: Exception Behavior

            Raises a :py:class:`blitzdb.document.Document.DoesNotExist` exception if no object with the given
            properties exists in the database, and a :py:class:`blitzdb.document.Document.MultipleObjectsReturned`
            exception if more than one object in the database corresponds to the given properties.

        """

    @abc.abstractmethod
    def delete(self, obj):
        """
        Deletes an object from the database.

        :param obj: The object to be deleted.
        """

    @abc.abstractmethod
    def filter(self, cls, **kwargs):
        """
        Filter objects from the database that correspond to a given set of
        properties.

        :param cls: The class for which to filter objects from the database.
        :param properties: The properties used to filter objects.
        :returns: A `blitzdb.queryset.QuerySet` instance containing the keys of the objects matching the query.

        .. admonition:: Functionality might differ between backends

             Please be aware that the functionality of the `filter` function might
             differ from backend to backend. Consult the documentation of the given
             backend that you use to find out which queries are supported.


        """
