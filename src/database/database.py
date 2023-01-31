import itertools
from typing import Callable, Optional, Type, Union
from datetime import datetime
import inspect

from .exceptions import KeyNotFound
from .record import (
    Record,

)
from .query import Query

from .base_class import LoadableDataclass

# from deta import Base
from ._local_base import Base as LocalBase

# Instructions for encoding / decoding data not supported by deta base
EMPTY_LIST_STRING = "$EMPTY_LIST"  # Setting a field to an empty list sets it to `null`
EMPTY_DICTIONARY_STRING = "$EMPTY_DICT"  # Setting a field to an empty dictionaries seems to set it to `null`
DATETIME_STRING = "$ENCODED_DATETIME"  # Ease datetime conversion, not that I really need of it for PTD though
ESCAPE_STRING = "$NOOP"  # Do not mess up if the user input 'just happen' to start with a $COMMAND


class Database(dict[str, Record]):
    def __init__(self, name: str, record_type: Type[Record]):
        """Deta Base wrapper | ORM
        
        Parameters
        ----------
        name : str
            Which name to use for the Deta Base
        record_type : RecordType
            Which type of Record to return.
            You may subclass the Record class or use it directly, though direct usage is not recommended
        """
        # TODO IMPLEMENT `MEMORY` / `DETA BASE` SUPPORT
        # base_mode = os.getenv("DETA_ECLIPSE_ORM_DATABASE_MODE", "DETA_BASE")
        # if base_mode == "DETA_BASE":
        #     self.__base = Base(name)
        # elif base_mode == "MEMORY":
        #     self.__base = LocalBase(name)
        # elif base_mode == "DISK":
        #     self.__base = LocalBase(name, sync_disk=True)
        # else:
        #     raise Exception("Invalid value for DETA_ECLIPSE_ORM_DATABASE_MODE environment variable")
        self.__base = LocalBase(name, sync_disk=True)
        
        self.__record_type = record_type
        self.load_record = record_type.from_dict

    def __getitem__(self, key: str) -> Record:
        """Retrieves an item from the database.
        If the key if not found, raises a KeyError.
        """
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    def __setitem__(self, key: str, record: Record) -> None:
        if not isinstance(record, Record):
            raise TypeError("You can only set database values to Record subclasses.")
        self.put(key, record.to_dict())
    
    def __delitem__(self, key: str) -> None:
        self.delete(key)

    def encode_entry(self, record: Union[dict, Record, LoadableDataclass]) -> dict:
        "Converts values so that we can store it properly in Deta Base. Does not modifies in-place."
        if isinstance(record, (Record, LoadableDataclass)):
            return self.encode_entry(record.to_dict())
        record = record.copy()
        for key, value in record.items():
            if isinstance(value, dict) and dict(value) == {}:  # Empty dict becomes `null` on deta base on update
                record[key] = EMPTY_DICTIONARY_STRING
            elif isinstance(value, (list, tuple)) and list(value) == []:  # Empty lists becomes `null` on deta base on update
                record[key] = EMPTY_LIST_STRING
            elif inspect.isfunction(value):  # Converts functions to references based on their source file and name
                if value in self.__known_functions:
                    record[key] = {
                        "__database_load_method": "function",
                        "__name": self.__known_functions[value],
                    }
                else:
                    raise ValueError(f"""Unexpected function: {value}. \
                    If you want to save it in the database, please use the `@base.remember_function` decorator.""")
                # This should only be used if this record is only going to be stored for a short amount of time
                # And even then, it should be using sparingly
            elif isinstance(value, (dict, Record, LoadableDataclass)):  # Convert nested fields 
                record[key] = self.encode_entry(value)
            elif isinstance(value, list):  # Convert all list elements
                # NOTE: Currently won't work for nested lists
                record[key] = [
                    self.encode_entry(element)
                    if isinstance(element, (dict, LoadableDataclass)) else element 
                    for element in value
                ]
            elif isinstance(value, datetime):  # Ease datetime conversion
                record[key] = DATETIME_STRING + value.isoformat()
            elif isinstance(value, str) and value.startswith("$"):  # essentially escape '$'
                record[key] = ESCAPE_STRING + value
        return record

    def _load_encoded(self, record: dict) -> Union[LoadableDataclass, Callable]:
        "Tries to load back an encoded record field into a user defined class or function. May raise exceptions."
        method = record['__database_load_method']
        if method == "function":
            name = record['__name']
            return self.__known_functions[name]
        elif method == "LoadableDataclass.from_dict":
            cls = LoadableDataclass._known_database_models[record["__class_name"]]
            return cls.from_dict(record)

    def decode_entry(self, record: dict) -> Union[dict, LoadableDataclass]:
        "Converts back some changes that we may make when storing. May modify in-place."
        for key, value in record.items():
            if isinstance(value, dict):  # Make sure we hit nested fields
                record[key] = self.decode_entry(value)
            elif isinstance(value, list):  # Convert all list elements. NOTE: Currently won't work for nested lists
                record[key] = [
                    self.decode_entry(element) if isinstance(element, dict) else element
                    for element in value
                ]
            elif isinstance(value, str):  # Revert our custom 'special' strings
                if value == EMPTY_DICTIONARY_STRING:  # Empty dict may become `null` on deta base
                    record[key] = {}
                elif value == EMPTY_LIST_STRING:  # Empty lists may become `null` on deta base
                    record[key] = []
                elif value.startswith(DATETIME_STRING):  # Ease datetime conversion
                    record[key] = datetime.fromisoformat(value.removeprefix(DATETIME_STRING))
                elif value.startswith(ESCAPE_STRING):  # Escape strings starting with `$`
                    record[key] = value.removeprefix(ESCAPE_STRING)

        if '__database_load_method' in record:
            return self._load_encoded(record)
        return record

    def get(self, key: str) -> Optional[Record]:
        """Retrieve a record based on it's key. 
        If it does not exists, returns None"""
        data = self.__base.get(str(key))
        if data is None:
            return None
        return self.load_record(self.decode_entry(data))

    def insert(self, key: str, data: dict) -> Record:
        "Insert a record and return it."
        self.__base.insert(self.encode_entry(data), str(key))
        return self.load_record(data)
    
    def put(self, key: str, data: dict) -> Record:
        "Insert or update a record and return it."
        self.__base.put(self.encode_entry(data), str(key))
        return self.load_record(data)
    
    def delete(self, key: str) -> None:
        "Deletes a record."
        self.__base.delete(str(key))

    def _put_many_list(self, data: list[list[Union[dict, Record]]], key_source: Callable[[Union[dict, Record]], str]):
        _all_records = []
        for sublist in data:
            records = []
            for record in sublist:
                _key = key_source(record)
                if isinstance(record, Record):
                    record = record.to_dict()
                record = self.encode_entry(record)
                record['key'] = _key
                records.append(record)
            self.__base.put_many(records)
            _all_records.extend(records)
        return _all_records
    
    def _put_many_dict(self, data: list[tuple[str, Union[dict, Record]]]):
        _all_records = []
        for subdict in data:
            records = []
            for k, record in subdict:
                record = self.encode_entry(record)
                record['key'] = k
                records.append(record)
            self.__base.put_many(records)
            _all_records.extend(records)
        return _all_records


    def put_many(self,
        data: Union[list[Union[Record, dict]], dict],
        *,
        key_source: Union[str, Callable[[Union[Record, dict]], str]],
        iter: bool = False,
    ) -> list[Record]:
        """Insert or overwrite multiple records and return them. 
        Deta Base has a limit of up to 25 records at once without `iter`

        Parameters
        ----------
        data : list of records or dictionary
            If a list of records: All records them must have `key_field` set as an attribute or item
            If a dictionary: Uses the dictionary's key for each record
        key_source: str or function
            Which field to use as the `key` for each record in data.
            If it's callable, it will be called for each record
            Ignored when using a dictionary
        iter : bool, default False
            Automatically split the data into sublists of up to 25 items and put multiple times.
        """
        if isinstance(data, list):
            if iter:
                data_chain = itertools.chain(data[offset:offset+25] for offset in range(0, len(data), 25))
            else:
                data_chain = [data]
            if isinstance(key_source, str):
                key_field = key_source
                # NOTE: WE MIGHT WANT TO ALLOW FALSE-ISH VALUES FROM THE FIRST WITHOUT GOING TO THE __GETITEM__? E.G. 0
                key_source = lambda record: str(getattr(record, key_field, None) or record[key_field])
            _all_records = self._put_many_list(data_chain, key_source)
        elif isinstance(data, dict):
            if iter:
                _it = iter(data.items())
                data_chain = (itertools.islice(_it, 25) for _ in range(((len(data)-1) // 25) + 1))
            else:
                data_chain = [data.items()]
            _all_records = self._put_many_dict(data_chain)
        else:
            raise TypeError(f"Unsupported type {type(data)} passed to database.put_many: {data!r}")
        return [self.load_record(self.decode_entry(record)) for record in _all_records]

    def fetch(
        self,
        query: Union[Query, dict, list[dict], None] = None,
        limit: int = 1000,
        last: Optional[str] = None,
        follow_last: bool = False,
    ) -> list[Record]:
        """Returns multiple items from the database based on a query.

        Parameters
        ----------
        query : Query
            See the `Query` and `Field` classes as well as https://docs.deta.sh/docs/base/queries/ for more information.
        limit : int, default 1000
            Maximum number of records to fetch.
            NOTE: Deta Base will only retrieve up to 1MB of data at a time, and that is before applying the filters
        last : str, default None
            Equivalent to `offset` in normal databases, but key based instead of position based
        follow_last : bool, default False
            Automatically fetch more records up to `limit` if the query returns a `last` element
        """
        if isinstance(query, Query):
            query = query.to_list()
        result = self.__base.fetch(query, limit=limit, last=last)
        records = result.items
        if follow_last:
            while result.last is not None and len(records) < limit:
                result = self.__base.fetch(query, limit=limit, last=result.last)
                records.extend(result.items)

        return [
            self.load_record(self.decode_entry(record))
            for record in records
        ]

    def update(self, key: str, updates: dict) -> None:
        """Updates a Record in the database. Local representations of it might become outdated."""
        updates = self.encode_entry(updates)
        try:
            self.__base.update(updates, key)
        except Exception as err:
            import re
            reason = err.args[0] if err.args else ''
            if isinstance(reason, str) and re.fullmatch(r"Key \'.*\' not found", reason):
                raise KeyNotFound(reason)
            else:
                raise