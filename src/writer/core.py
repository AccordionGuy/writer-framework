import asyncio
import base64
import contextlib
import copy
import dataclasses
import datetime
import functools
import inspect
import io
import json
import logging
import math
import multiprocessing
import numbers
import os
import re
import secrets
import time
import traceback
import urllib.request
from abc import ABCMeta
from contextvars import ContextVar
from multiprocessing.process import BaseProcess
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Generator,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

import pyarrow  # type: ignore

import writer.workflows
from writer import core_ui
from writer.core_ui import Component
from writer.ss_types import (
    DataframeRecordAdded,
    DataframeRecordRemoved,
    DataframeRecordUpdated,
    InstancePath,
    InstancePathItem,
    Readable,
    ServeMode,
    WorkflowExecutionLog,
    WriterEvent,
    WriterEventResult,
    WriterFileItem,
)

if TYPE_CHECKING:
    import pandas
    import polars

    from writer.app_runner import AppProcess
    from writer.ss_types import AppProcessServerRequest

@dataclasses.dataclass
class CurrentRequest:
    session_id: str
    request: 'AppProcessServerRequest'

_current_request: ContextVar[Optional[CurrentRequest]] = ContextVar("current_request", default=None)

@contextlib.contextmanager
def use_request_context(session_id: str, request: 'AppProcessServerRequest'):
    """
    Context manager to set the current request context.

    >>> session_id = "xxxxxxxxxxxxxxxxxxxxxxxxx"
    >>> request = AppProcessServerRequest(type='event', payload=EventPayload(event='my_event'))
    >>> with use_request_context(session_id, request):
    >>>     pass
    """
    try:
        _current_request.set(CurrentRequest(session_id, request))
        yield
    finally:
        _current_request.set(None)

def get_app_process() -> 'AppProcess':
    """
    Retrieves the Writer Framework process context.

    >>> _current_process = get_app_process()
    >>> _current_process.bmc_components # get the component tree
    """
    from writer.app_runner import AppProcess  # Needed during runtime
    raw_process: BaseProcess = multiprocessing.current_process()
    if isinstance(raw_process, AppProcess):
        return raw_process

    raise RuntimeError( "Failed to retrieve the AppProcess: running in wrong context")


def import_failure(rvalue: Any = None):
    """
    This decorator captures the failure to load a volume and returns a value instead.

    If the import of a module fails, the decorator returns the value given as a parameter.

    >>> @import_failure(rvalue=False)
    >>> def my_handler():
    >>>     import pandas
    >>>     return pandas.DataFrame()

    :param rvalue: the value to return
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except ImportError:
                return rvalue
        return wrapper
    return decorator


class Config:

    is_mail_enabled_for_log: bool = False
    mode: ServeMode = "run"
    logger: Optional[logging.Logger] = None
    feature_flags: list[str] = []

@dataclasses.dataclass
class MutationSubscription:
    """
    Describes a subscription to a mutation.

    The path on which this subscription is subscribed and the handler
    to execute when a mutation takes place on this path.

    >>> def myhandler(state):
    >>>     state["b"] = state["a"]

    >>> m = MutationSubscription(path="a.c", handler=myhandler)
    """
    type: Literal['subscription', 'property']
    path: str
    handler: Callable # Handler to execute when mutation happens
    state: 'State'
    property_name: Optional[str] = None

    def __post_init__(self):
        if len(self.path) == 0:
            raise ValueError("path cannot be empty.")
        
        path_parts = parse_state_variable_expression(self.path)
        for part in path_parts:
            if len(part) == 0:
                raise ValueError(f"path {self.path} cannot have empty parts.")

    @property
    def local_path(self) -> str:
        """
        Returns the last part of the key to monitor on the state

        >>> m = MutationSubscription(path="a.c", handler=myhandler)
        >>> m.local_path
        >>> "c"
        """
        path_parts = parse_state_variable_expression(self.path)
        return path_parts[-1]

class StateRecursionWatcher():
    limit = 128

    def __init__(self):
        self.counter_recursion = 0

_state_recursion_watcher = ContextVar("state_recursion_watcher", default=StateRecursionWatcher())

@contextlib.contextmanager
def state_recursion_new(key: str):
    """
    Context manager to watch the state recursion on mutation subscriptions.

    The context throws a RecursionError exception if more than 128 cascading mutations
    are performed on the same state
    """
    recursion_watcher = _state_recursion_watcher.get()
    try:
        recursion_watcher.counter_recursion += 1
        if recursion_watcher.counter_recursion > recursion_watcher.limit:
            raise RecursionError(f"State Recursion limit reached {recursion_watcher.limit}.")
        yield
    finally:
        recursion_watcher.counter_recursion -= 1

class FileWrapper:
    """
    A wrapper for either a string pointing to a file or a file-like object with a read() method.
    Provides a method for retrieving the data as data URL.
    Allows for convenient serialisation of files.
    """

    def __init__(self, file: Union[Readable, str], mime_type: Optional[str] = None):
        if not file:
            raise ValueError("Must specify a file.")
        if not (
                callable(getattr(file, "read", None)) or
                isinstance(file, str)):
            raise ValueError(
                "File must provide a read() method or contain a string with a path to a local file.")
        self.file = file
        self.mime_type = mime_type

    def _get_file_stream_as_dataurl(self, f_stream: Readable) -> str:
        base64_str = base64.b64encode(f_stream.read()).decode("latin-1")
        dataurl = f"data:{self.mime_type if self.mime_type is not None else ''};base64,{ base64_str }"
        return dataurl

    def get_as_dataurl(self) -> str:
        if isinstance(self.file, str):
            with open(self.file, "rb") as f_stream:
                return self._get_file_stream_as_dataurl(f_stream)
        elif callable(getattr(self.file, "read", None)):
            return self._get_file_stream_as_dataurl(self.file)
        else:
            raise ValueError("Invalid file.")


class BytesWrapper:

    """
    A wrapper for raw byte data.
    Provides a method for retrieving the data as data URL.
    Allows for convenient serialisation of byte data.
    """

    def __init__(self, raw_data: Any, mime_type: Optional[str] = None):
        self.raw_data = raw_data
        self.mime_type = mime_type

    def get_as_dataurl(self) -> str:
        b64_data = base64.b64encode(self.raw_data).decode("utf-8")
        durl = f"data:{self.mime_type if self.mime_type is not None else ''};base64,{ b64_data }"
        return durl


class StateSerialiserException(ValueError):
    pass


class StateSerialiser:
    """
    Serialises user state values before sending them to the front end.
    Provides JSON-compatible values, including data URLs for binary data.
    """
    def serialise(self, v: Any) -> Union[Dict, List, str, bool, int, float, None]:
        from writer.ai import Conversation
        if isinstance(v, State):
            return self._serialise_dict_recursively(v.to_dict())
        if isinstance(v, Conversation):
            return v.serialized_messages
        if isinstance(v, (FileWrapper, BytesWrapper)):
            return self._serialise_ss_wrapper(v)
        if isinstance(v, (datetime.datetime, datetime.date)):
            return str(v)
        if isinstance(v, bytes):
            return self.serialise(BytesWrapper(v))
        if isinstance(v, dict):
            return self._serialise_dict_recursively(v)
        if isinstance(v, list):
            return self._serialise_list_recursively(v)
        if isinstance(v, (str, bool)):
            return v
        if isinstance(v, EditableDataframe):
            table = v.pyarrow_table()
            return self._serialise_pyarrow_table(table)
        if v is None:
            return v

        # Checking the MRO allows to determine object type without creating dependencies
        # to these packages

        v_mro = [
            f"{x.__module__}.{x.__name__}" for x in inspect.getmro(type(v))]

        if isinstance(v, (int, float)):
            if "numpy.float64" in v_mro:
                return float(v)
            if math.isnan(v):
                return None
            return v

        if "pandas.core.frame.DataFrame" in v_mro:
            return self._serialise_pandas_dataframe(v)
        if hasattr(v, "__dataframe__"):
            return self._serialize_dataframe(v)

        if "matplotlib.figure.Figure" in v_mro:
            return self._serialise_matplotlib_fig(v)
        if "plotly.graph_objs._figure.Figure" in v_mro:
            return v.to_json()
        if "numpy.float64" in v_mro:
            return float(v)
        if "numpy.ndarray" in v_mro:
            return self._serialise_list_recursively(v.tolist())
        if "pyarrow.lib.Table" in v_mro:
            return self._serialise_pyarrow_table(v)

        if hasattr(v, "to_dict") and callable(v.to_dict):
            # Covers Altair charts, Plotly graphs
            return self._serialise_dict_recursively(v.to_dict())

        raise StateSerialiserException(
            f"Object of type { type(v) } (MRO: {v_mro}) cannot be serialised.")

    def _serialise_dict_recursively(self, d: Dict) -> Dict:
        return {str(k): self.serialise(v) for k, v in d.items()}

    def _serialise_list_recursively(self, l: List) -> List:  # noqa: E741
        return [self.serialise(v) for v in l]

    def _serialise_ss_wrapper(self, v: Union[FileWrapper, BytesWrapper]) -> str:
        return v.get_as_dataurl()

    def _serialise_matplotlib_fig(self, fig) -> str:
        # It's safe to import matplotlib here without listing it as a dependency.
        # If this method is called, it's because a matplotlib figure existed.
        # Note: matplotlib type needs to be ignored since it doesn't provide types
        import matplotlib.pyplot as plt  # type: ignore

        iobytes = io.BytesIO()
        fig.savefig(iobytes, format="png")
        iobytes.seek(0)
        plt.close(fig)
        return FileWrapper(iobytes, "image/png").get_as_dataurl()

    def _serialize_dataframe(self, df) -> str:
        """
        Serialize a dataframe with pyarrow a dataframe that implements
        the Dataframe Interchange Protocol i.e. the __dataframe__() method

        :param df: dataframe that implements Dataframe Interchange Protocol (__dataframe__ method)
        :return: a arrow file as a dataurl (application/vnd.apache.arrow.file)
        """
        import pyarrow.interchange  # type: ignore
        table = pyarrow.interchange.from_dataframe(df)
        return self._serialise_pyarrow_table(table)

    def _serialise_pandas_dataframe(self, df):
        import pyarrow as pa  # type: ignore
        pa_table = pa.Table.from_pandas(df, preserve_index=True)
        return self._serialise_pyarrow_table(pa_table)

    def _serialise_pyarrow_table(self, table):
        import pyarrow as pa  # type: ignore

        sink = pa.BufferOutputStream()
        batches = table.to_batches()
        with pa.ipc.new_file(sink, table.schema) as writer:
            for batch in batches:
                writer.write_batch(batch)
        buf = sink.getvalue()
        bw = BytesWrapper(buf, "application/vnd.apache.arrow.file")
        return self.serialise(bw)

class MutableValue:
    """
    MutableValue allows you to implement a value whose modification
    will be followed by the state of Writer Framework and will trigger the refresh
    of the user interface.

    >>> class MyValue(MutableValue):
    >>>     def __init__(self, value):
    >>>         self.value = value
    >>>
    >>>     def modify(self, new_value):
    >>>         self.value = new_value
    >>>         self.mutate()
    """
    def __init__(self):
        self._mutated = False

    def mutated(self) -> bool:
        """
        Returns whether the value has been mutated.
        :return:
        """
        return self._mutated

    def mutate(self) -> None:
        """
        Marks the value as mutated.
        This will trigger the refresh of the user interface on the next round trip
        :return:
        """
        self._mutated = True

    def reset_mutation(self) -> None:
        """
        Resets the mutation flag to False.
        :return:
        """
        self._mutated = False

class StateProxy:

    """
    The root user state and its children (nested states) are instances of this class.
    Provides proxy functionality to detect state mutations via assignment.
    """

    def __init__(self, raw_state: Dict = {}):
        self.state: Dict[str, Any] = {}
        self.local_mutation_subscriptions: List[MutationSubscription] = []
        self.initial_assignment = True
        self.mutated: Set[str] = set()
        self.ingest(raw_state)

    def __repr__(self) -> str:
        return self.state.__repr__()

    def __contains__(self, key: str) -> bool:
        return self.state.__contains__(key)

    def ingest(self, raw_state: Dict) -> None:
        for key, raw_value in raw_state.items():
            self.__setitem__(key, raw_value)

    def items(self) -> Sequence[Tuple[str, Any]]:
        return cast(Sequence[Tuple[str, Any]], self.state.items())

    def get(self, key: str) -> Any:
        return self.state.get(key)

    def __getitem__(self, key: str) -> Any:
        return self.state.get(key)

    def __setitem__(self, key: str, raw_value: Any) -> None:
        with state_recursion_new(key):
            if not isinstance(key, str):
                raise ValueError(
                    f"State keys must be strings. Received {str(key)} ({type(key)}).")
            old_value = self.state.get(key)
            self.state[key] = raw_value

            for local_mutation in self.local_mutation_subscriptions:
                if local_mutation.local_path == key:
                    if local_mutation.type == 'subscription':
                        context_data = {
                            "event": "mutation",
                            "mutation": local_mutation.path
                        }
                        payload = {
                            "previous_value": old_value,
                            "new_value": raw_value
                        }

                        writer_event_handler_invoke(local_mutation.handler, {
                            "state": local_mutation.state,
                            "context": context_data,
                            "payload": payload,
                            "session": _event_handler_session_info(),
                            "ui": _event_handler_ui_manager()
                        })
                    elif local_mutation.type == 'property':
                        assert local_mutation.property_name is not None
                        self[local_mutation.property_name] = local_mutation.handler(local_mutation.state)

            self._apply_raw(f"+{key}")

    def __delitem__(self, key: str) -> None:
        if key in self.state:
            del self.state[key]
            self._apply_raw(f"-{key}")  # Using "-" prefix to indicate deletion

    def remove(self, key: str) -> None:
        return self.__delitem__(key)

    def _apply_raw(self, key: str) -> None:
        self.mutated.add(key)

    def apply_mutation_marker(self, key: Optional[str] = None, recursive: bool = False) -> None:
        """
        Adds the mutation marker to a state. The mutation marker is used to track changes in the state.

        >>> self.apply_mutation_marker()

        Add the mutation marker on a specific field

        >>> self.apply_mutation_marker("field")

        Add the mutation marker to a state and all of its children

        >>> self.apply_mutation_marker(recursive=True)
        """
        keys = [key] if key is not None else self.state.keys()

        for k in keys:
            self._apply_raw(f"+{k}")
            if recursive is True:
                value = self.state[k]
                if isinstance(value, StateProxy):
                    value.apply_mutation_marker(recursive=True)

    @staticmethod
    def escape_key(key):
        return key.replace(".", r"\.")

    def get_mutations_as_dict(self) -> Dict[str, Any]:
        serialised_mutations: Dict[str, Union[Dict, List, str, bool, int, float, None]] = {}

        def carry_mutation_flag(base_key, child_key):
            child_mutation_flag, child_key = child_key[0], child_key[1:]
            return f"{child_mutation_flag}{base_key}.{child_key}"

        for key, value in list(self.state.items()):
            if key.startswith("_"):
                continue
            
            escaped_key = self.escape_key(key)
            serialised_value = None

            if isinstance(value, StateProxy):
                if f"+{key}" in self.mutated:
                    serialised_mutations[f"+{escaped_key}"] = serialised_value
                value.initial_assignment = False
                child_mutations = value.get_mutations_as_dict()
                if child_mutations is None:
                    continue
                for child_key, child_mutation in child_mutations.items():
                    nested_key = carry_mutation_flag(escaped_key, child_key)
                    serialised_mutations[nested_key] = child_mutation
            elif f"+{key}" in self.mutated:
                try:
                    serialised_value = state_serialiser.serialise(value)
                except BaseException:
                    raise ValueError(f"""Couldn't serialise value of type "{ type(value) }" for key "{ key }".""")
                serialised_mutations[f"+{escaped_key}"] = serialised_value
            elif isinstance(value, MutableValue) is True and value.mutated():
                try:
                    serialised_value = state_serialiser.serialise(value)
                    value.reset_mutation()
                except BaseException:
                    raise ValueError(f"""Couldn't serialise value of type "{ type(value) }" for key "{ key }".""")
                serialised_mutations[f"+{escaped_key}"] = serialised_value

        deleted_keys = \
            {self.escape_key(key)
                for key in self.mutated
                if key.startswith("-")}
        for key in deleted_keys:
            serialised_mutations[f"{key}"] = None

        self.mutated = set()
        return serialised_mutations

    def to_dict(self) -> Dict[str, Any]:
        serialised = {}
        for key, value in self.state.items():
            if key.startswith("_"):
                continue
            serialised_value = None
            try:
                serialised_value = state_serialiser.serialise(value)
            except BaseException:
                raise ValueError(
                    f"""Couldn't serialise value of type "{ type(value) }" for key "{ key }".""")
            serialised[key] = serialised_value
        return serialised

    def to_raw_state(self):
        """
        Converts a StateProxy and its children into a python dictionary.

        >>> state = State({'a': 1, 'c': {'a': 1, 'b': 3}})
        >>> _raw_state = state._state_proxy.to_raw_state()
        >>> {'a': 1, 'c': {'a': 1, 'b': 3}}

        :return: a python dictionary that represents the raw state
        """
        raw_state = {}
        for key, value in self.state.items():
            if isinstance(value, StateProxy):
                value = value.to_raw_state()
            raw_state[key] = value

        return raw_state


def get_annotations(instance) -> Dict[str, Any]:
    """
    Returns the annotations of the class in a way that works on python 3.9 and python 3.10
    """
    if isinstance(instance, type):
        ann = instance.__dict__.get('__annotations__', None)
    else:
        ann = getattr(instance, '__annotations__', None)

    if ann is None:
        ann = {}
    return ann

class StateMeta(type):
    """
    Constructs a class at runtime that extends WriterState or State
    with dynamic properties for each annotation of the class.
    """

    def __new__(cls, name, bases, attrs):
        klass = super().__new__(cls, name, bases, attrs)
        cls.bind_annotations_to_state_proxy(klass)
        return klass

    @classmethod
    def bind_annotations_to_state_proxy(cls, klass):
        """
        Loops through the class annotations and creates properties dynamically for each one.

        >>> class MyState(State):
        >>>     counter: int

        will be transformed into

        >>> class MyState(State):
        >>>
        >>>     @property
        >>>     def counter(self):
        >>>         return self._state_proxy["counter"]
        >>>
        >>>    @counter.setter
        >>>    def counter(self, value):
        >>>        self._state_proxy["counter"] = value

        Annotations that reference a State are ignored. The link will be established through a State instance
        when ingesting state data.

        >>> class MyAppState(State):
        >>>     title: str

        >>> class MyState(State):
        >>>     myapp: MyAppState # Nothing happens
        """

        annotations = get_annotations(klass)
        for key, expected_type in annotations.items():
            if key == "_state_proxy":
                raise AttributeError("_state_proxy is an reserved keyword for Writer Framework, don't use it in annotation.")

            if not(inspect.isclass(expected_type) and issubclass(expected_type, State)):
                proxy = DictPropertyProxy("_state_proxy", key)
                setattr(klass, key, proxy)

class State(metaclass=StateMeta):

    def __init__(self, raw_state: Optional[Dict[str, Any]] = None):
        final_raw_state = raw_state if raw_state is not None else {}

        self._state_proxy: StateProxy = StateProxy(final_raw_state)
        self.ingest(final_raw_state)

        # This step saves the properties associated with the instance
        for attribute in calculated_properties_per_state_type.get(self.__class__, []):
            getattr(self, attribute)

    def ingest(self, raw_state: Dict[str, Any]) -> None:
        """
        hydrates a state from raw data by applying a schema when it is provided.
        The existing content in the state is erased.


        >>> state = WriterState({'message': "hello world"})
        >>> state.ingest({'a': 1, 'b': 2})
        >>> {'a': 1, 'b': 2}
        """
        self._state_proxy.state = {}
        for key, value in raw_state.items():
            assert not isinstance(value, StateProxy), f"state proxy datatype is not expected in ingest operation, {locals()}"
            self._set_state_item(key, value)

    def to_dict(self) -> dict:
        """
        Serializes state data as a dictionary

        Private attributes, prefixed with _, are ignored.

        >>> state = WriterState({'message': "hello world"})
        >>> return state.to_dict()
        """
        return self._state_proxy.to_dict()


    def to_raw_state(self) -> dict:
        """
        Converts a StateProxy and its children into a python dictionary that can be used to recreate the
        state from scratch.

        >>> state = WriterState({'a': 1, 'c': {'a': 1, 'b': 3}})
        >>> raw_state = state.to_raw_state()
        >>> "{'a': 1, 'c': {'a': 1, 'b': 3}}"

        :return: a python dictionary that represents the raw state
        """
        return self._state_proxy.to_raw_state()

    def __repr__(self) -> str:
        return self._state_proxy.__repr__()

    def __getitem__(self, key: str) -> Any:

        # Essential to support operation like
        # state['item']['a'] = state['item']['b']
        if hasattr(self, key):
            value = getattr(self, key)
            if isinstance(value, State):
                return value

        return self._state_proxy.__getitem__(key)

    def __setitem__(self, key: str, raw_value: Any) -> None:
        assert not isinstance(raw_value, StateProxy), f"state proxy datatype is not expected, {locals()}"

        self._set_state_item(key, raw_value)

    def __delitem__(self, key: str) -> Any:
        return self._state_proxy.__delitem__(key)

    def remove(self, key: str) -> Any:
        return self.__delitem__(key)

    def items(self) -> Generator[Tuple[str, Any], None, None]:
        for k, v in self._state_proxy.items():
            if isinstance(v, StateProxy):
                # We don't want to expose StateProxy to the user, so
                # we replace it with relative State
                yield k, getattr(self, k)
            else:
                yield k, v

    def __contains__(self, key: str) -> bool:
        return self._state_proxy.__contains__(key)

    def _set_state_item(self, key: str, value: Any):
        """
        """

        """
        At this level, the values that arrive are either States which encapsulate a StateProxy, or another datatype. 
        If there is a StateProxy, it is a fault in the code.
        """
        annotations = get_annotations(self)
        expected_type = annotations.get(key, None)
        expect_dict = expected_type is not None and inspect.isclass(expected_type) and issubclass(expected_type, dict)
        if isinstance(value, dict) and not expect_dict:
            """
            When the value is a dictionary and the attribute does not explicitly 
            expect a dictionary, we instantiate a new state to manage mutations.
            """
            state = annotations[key](value) if key in annotations else State()
            if not isinstance(state, State):
                raise ValueError(f"Attribute {key} must inherit of State or requires a dict to accept dictionary")

            setattr(self, key, state)
            state.ingest(value)
            self._state_proxy[key] = state._state_proxy
        else:
            if isinstance(value, State):
                value._state_proxy.apply_mutation_marker(recursive=True)
                self._state_proxy[key] = value._state_proxy
            else:
                self._state_proxy[key] = value


    def subscribe_mutation(self,
                           path: Union[str, List[str]],
                           handler: Callable[..., Union[None, Awaitable[None]]],
                           initial_triggered: bool = False) -> None:
        r"""
        Automatically triggers a handler when a mutation occurs in the state.

        >>> def _increment_counter(state):
        >>>     state['my_counter'] += 1
        >>>
        >>> state = WriterState({'a': 1, 'c': {'a': 1, 'b': 3}, 'my_counter': 0})
        >>> state.subscribe_mutation('a', _increment_counter)
        >>> state.subscribe_mutation('c.a', _increment_counter)
        >>> state['a'] = 2 # will trigger _increment_counter
        >>> state['a'] = 3 # will trigger _increment_counter
        >>> state['c']['a'] = 2 # will trigger _increment_counter

        subscribe mutation accepts the signature of an event handler.

        >>> def _increment_counter(state, payload, context, session, ui):
        >>>     state['my_counter'] += 1
        >>>
        >>> state = WriterState({'a': 1, 'my_counter': 0})
        >>> state.subscribe_mutation('a', _increment_counter)
        >>> state['a'] = 2 # will trigger _increment_counter

        subscribe mutation accepts escaped dot expressions to encode key that contains `dot` separator

        >>> def _increment_counter(state, payload, context, session, ui):
        >>>     state['my_counter'] += 1
        >>>
        >>> state = WriterState({'a.b': 1, 'my_counter': 0})
        >>> state.subscribe_mutation('a\.b', _increment_counter)
        >>> state['a.b'] = 2 # will trigger _increment_counter

        :param path: path of mutation to monitor
        :param func: handler to call when the path is mutated
        """
        if isinstance(path, str):
            path_list = [path]
        else:
            path_list = path

        for p in path_list:
            state_proxy = self._state_proxy
            path_parts = parse_state_variable_expression(p)
            for i, path_part in enumerate(path_parts):
                if i == len(path_parts) - 1:
                    local_mutation = MutationSubscription('subscription', p, handler, self)
                    state_proxy.local_mutation_subscriptions.append(local_mutation)

                    # At startup, the application must be informed of the
                    # existing states. To cause this, we trigger manually
                    # the handler.
                    if initial_triggered is True:
                        writer_event_handler_invoke(handler, {
                            "state": self,
                            "context": {"event": "init"},
                            "payload": {},
                            "session": {},
                            "ui": _event_handler_ui_manager()
                        })

                elif path_part in state_proxy:
                    state_proxy = state_proxy[path_part]
                else:
                    raise ValueError(f"Mutation subscription failed - {p} not found in state")

    def calculated_property(self,
                            property_name: str,
                            path: Union[str, List[str]],
                            handler: Callable[..., Union[None, Awaitable[None]]]) -> None:
        """
        Update a calculated property when a mutation triggers

        This method is dedicated to be used through a calculated property. It is not
        recommended to invoke it directly.

        >>> class MyState(State):
        >>>     title: str
        >>>
        >>>     wf.property('title')
        >>>     def title_upper(self):
        >>>         return self.title.upper()

        Usage
        =====

        >>> state = wf.init_state({'title': 'hello world'})
        >>> state.calculated_property('title_upper', 'title', lambda state: state.title.upper())
        """
        if isinstance(path, str):
            path_list = [path]
        else:
            path_list = path

        for p in path_list:
            state_proxy = self._state_proxy
            path_parts = parse_state_variable_expression(p)
            for i, path_part in enumerate(path_parts):
                if i == len(path_parts) - 1:
                    local_mutation = MutationSubscription('property', p, handler, self, property_name)
                    state_proxy.local_mutation_subscriptions.append(local_mutation)
                    state_proxy[property_name] = handler(self)
                elif path_part in state_proxy:
                    state_proxy = state_proxy[path_part]
                else:
                    raise ValueError(f"Property subscription failed - {p} not found in state")


class WriterState(State):
    """
    Root state. Comprises user configurable state and
    mail (notifications, log entries, etc).
    """

    LOG_ENTRY_MAX_LEN = 8192

    def __init__(self, raw_state: Dict[str, Any] = {}, mail: List[Any] = []):
        super().__init__(raw_state)
        self.mail = copy.deepcopy(mail)

    @property
    def user_state(self) -> StateProxy:
        return self._state_proxy

    @classmethod
    def get_new(cls):
        """ Returns a new WriterState instance set to the initial state."""

        return initial_state.get_clone()

    def get_clone(self) -> 'WriterState':
        """
        get_clone clones the destination application state for the session.

        The class is rebuilt identically in the case where the user
        has constructed a schema inherited from WriterState

        >>> class AppSchema(WriterState):
        >>>     counter: int
        >>>
        >>> root_state = AppSchema({'counter': 1})
        >>> clone_state = root_state.get_clone() # instance of AppSchema
        """
        try:
            cloned_user_state = copy.deepcopy(self.user_state.to_raw_state())
            cloned_mail = copy.deepcopy(self.mail)
        except BaseException:
            substitute_state = WriterState()
            substitute_state.add_log_entry("error",
                                           "Cannot clone state",
                                           "The state may contain unpickable objects, such as modules.",
                                           traceback.format_exc())
            return substitute_state

        cloned_state = self.__class__(cloned_user_state, cloned_mail)
        _clone_mutation_subscriptions(cloned_state, self)
        return cloned_state

    def add_mail(self, type: str, payload: Any) -> None:
        mail_item = {
            "type": type,
            "payload": payload
        }
        self.mail.insert(0, mail_item)

    def add_notification(self, type: Literal["info", "success", "warning", "error"], title: str, message: str) -> None:
        self.add_mail("notification", {
            "type": type,
            "title": title,
            "message": message,
        })

    def _log_entry_in_logger(self, type: Literal["debug", "info", "warning", "error", "critical"], title: str, message: str, code: Optional[str] = None) -> None:
        if not Config.logger:
            return
        log_args: Tuple[str, ...] = ()

        if code:
            log_args = (title, message, code)
        else:
            log_args = (title, message)

        log_colors = {
            "debug": "\x1b[36;20m",    # Cyan for debug
            "info": "\x1b[34;20m",     # Blue for info
            "warning": "\x1b[33;20m",  # Yellow for warning
            "error": "\x1b[31;20m",    # Red for error
            "critical": "\x1b[35;20m"  # Magenta for critical
        }

        log_methods = {
            "debug": Config.logger.debug,
            "info": Config.logger.info,
            "warning": Config.logger.warning,
            "error": Config.logger.error,
            "critical": Config.logger.critical
        }

        log_message = "From app log: " + ("\n%s" * len(log_args))

        color = log_colors.get(type, "\x1b[0m")  # Default to no color if type not found
        log_method = log_methods.get(type, Config.logger.info)  # Default to info level if type not found

        log_method(f"{color}{log_message}\x1b[0m", *log_args)

    def add_log_entry(self, type: Literal["info", "error"], title: str, message: str, code: Optional[str] = None, workflow_execution: Optional[WorkflowExecutionLog] = None) -> None:
        self._log_entry_in_logger(type, title, message, code)
        if not Config.is_mail_enabled_for_log:
            return
        shortened_message = None
        if len(message) > WriterState.LOG_ENTRY_MAX_LEN:
            shortened_message = message[0:WriterState.LOG_ENTRY_MAX_LEN] + "..."
        else:
            shortened_message = message
        self.add_mail("logEntry", {
            "type": type,
            "title": title,
            "message": shortened_message,
            "code": code,
            "workflowExecution": workflow_execution
        })

    def file_download(self, data: Any, file_name: str):
        if not isinstance(data, (bytes, FileWrapper, BytesWrapper)):
            raise ValueError(
                "Data for a fileDownload mail must be bytes, a FileWrapper or a BytesWrapper.")
        self.add_mail("fileDownload", {
            "data": state_serialiser.serialise(data),
            "fileName": file_name
        })

    def open_url(self, url: str):
        self.add_mail("openUrl", url)

    def clear_mail(self) -> None:
        self.mail = []

    def set_page(self, active_page_key: str) -> None:
        self.add_mail("pageChange", active_page_key)

    def set_route_vars(self, route_vars: Dict[str, str]) -> None:
        self.add_mail("routeVarsChange", route_vars)

    def import_stylesheet(self, stylesheet_key: str, path: str) -> None:
        self.add_mail("importStylesheet", {
            "stylesheetKey": stylesheet_key,
            "path": path
        })

    def import_script(self, script_key: str, path: str) -> None:
        """
        imports the content of a script into the page

        >>> initial_state = wf.init_state({
        >>>     "counter": 1
        >>> })
        >>>
        >>> initial_state.import_script("my_script", "/static/script.js")
        """
        self.add_mail("importScript", {
            "scriptKey": script_key,
            "path": path
        })

    def import_frontend_module(self, module_key: str, specifier: str) -> None:
        self.add_mail("importModule", {
            "moduleKey": module_key,
            "specifier": specifier
        })

    def call_frontend_function(self, module_key: str, function_name: str, args: List) -> None:
        self.add_mail("functionCall", {
            "moduleKey": module_key,
            "functionName": function_name,
            "args": args
        })

class MiddlewareExecutor():
    """
    A MiddlewareExecutor executes middleware in a controlled context. It allows writer framework
    to manage different implementations of middleware.

    Case 1 : A middleware is a generator, then run before and after code

    >>> @wf.middleware()
    >>> def my_middleware():
    >>>     print("before event handler")
    >>>     yield()
    >>>     print("after event handler")

    Case 2 : A middleware is just a function, then run the function before

    >>> @wf.middleware()
    >>> def my_middleware():
    >>>     print("before event handler")
    """

    def __init__(self, middleware: Callable):
        self.middleware = middleware

    @contextlib.contextmanager
    def execute(self, args: dict):
        middleware_args = writer_event_handler_build_arguments(self.middleware, args)
        it = self.middleware(*middleware_args)
        try:
            yield from it
        except StopIteration:
            yield
        except TypeError:
            yield


class MiddlewareRegistry:

    def __init__(self) -> None:
        self.registry: List[MiddlewareExecutor] = []

    def register(self, middleware: Callable):
        me = MiddlewareExecutor(middleware)
        self.registry.append(me)

    def executors(self) -> List[MiddlewareExecutor]:
        """
        Retrieves middlewares prepared for execution

        >>> executors = middleware_registry.executors()
        >>> result = writer_event_handler_invoke_with_middlewares(executors, lambda state: pass, {'state': {}, 'payload': {}})
        """
        return self.registry

class EventHandlerRegistry:
    """
    Maps functions registered as event handlers from the user app's core
    and external modules, providing an access mechanism to these maps.
    """

    class HandlerMeta(TypedDict):
        name: str
        args: List[str]

    class HandlerEntry(TypedDict):
        callable: Callable
        meta: 'EventHandlerRegistry.HandlerMeta'

    def __init__(self):
        self.handler_map: Dict[str, 'EventHandlerRegistry.HandlerEntry'] = {}  # type: ignore

    def __iter__(self):
        return iter(self.handler_map.keys())

    def register_handler(self, handler: Callable):
        module_name = handler.__module__

        # Prepare "access name"
        # (i.e. the key that frontend uses to retrieve handler)
        if module_name == "writeruserapp":
            # Use the handler's __qualname__ directly
            # for functions from main.py in user's app
            access_name = handler.__qualname__
        else:
            # For external handlers, separate the module name
            # and handler __qualname__by a dot
            access_name = f"{module_name}.{handler.__qualname__}"

        entry: EventHandlerRegistry.HandlerEntry = \
            {
                "callable": handler,
                "meta": {
                    "name": access_name,
                    "args": inspect.getfullargspec(handler).args
                }
            }

        self.handler_map[access_name] = entry

    def register_module(self, module: ModuleType):
        if isinstance(module, ModuleType):
            all_fn_names = (x[0] for x in inspect.getmembers(
                module, inspect.isfunction))
            exposed_fn_names = list(
                filter(lambda x: not x.startswith("_"), all_fn_names))

            for fn_name in exposed_fn_names:
                fn_callable = getattr(module, fn_name)
                if not fn_callable:
                    continue
                self.register_handler(fn_callable)
        else:
            raise ValueError(
                f"Attempted to register a non-module object: {module}"
                )

    def find_handler_callable(self, handler_name: str) -> Optional[Callable]:
        if handler_name not in self.handler_map:
            return None
        handler_entry: EventHandlerRegistry.HandlerEntry = \
            self.handler_map[handler_name]
        return handler_entry["callable"]

    def get_handler_meta(
            self,
            handler_name: str
            ) -> "EventHandlerRegistry.HandlerMeta":
        if handler_name not in self.handler_map:
            raise RuntimeError(f"Handler {handler_name} is not registered")
        entry: EventHandlerRegistry.HandlerEntry = \
            self.handler_map[handler_name]
        return entry["meta"]

    def gather_handler_meta(self) -> List["EventHandlerRegistry.HandlerMeta"]:
        return [self.get_handler_meta(handler_name) for handler_name in self]


class EventDeserialiser:

    """Applies transformations to the payload of an incoming event, depending on its type.

    The transformation happens in place: the event passed to the transform method is mutated.

    Its main goal is to deserialise incoming content in a controlled and predictable way,
    applying sanitisation of inputs where relevant."""

    def __init__(self, session_state: WriterState, session_component_tree: core_ui.ComponentTree):
        self.evaluator = Evaluator(session_state, session_component_tree)

    def transform(self, ev: WriterEvent) -> None:
        # Events without payloads are safe
        # This includes non-custom events such as click
        # Events not natively provided by Writer Framework aren't sanitised

        if ev.payload is None or not ev.type.startswith("wf-"):
            return

        # Look for a method in this class that matches the event type
        # As a security measure, all event types starting with "wf-" must be linked to a transformer function.

        custom_event_name = ev.type[3:]
        func_name = "_transform_" + custom_event_name.replace("-", "_")
        if not hasattr(self, func_name):
            ev.payload = {}
            raise ValueError(
                "No payload transformer available for custom event type.")
        tf_func = getattr(self, func_name)
        try:
            tf_payload = tf_func(ev)
        except BaseException:
            ev.payload = {}
            raise RuntimeError("Payload transformation failed.")
        else:
            ev.payload = tf_payload

    def _transform_tag_click(self, ev: WriterEvent) -> Optional[str]:
        payload = ev.payload
        instance_path = ev.instancePath
        options = self.evaluator.evaluate_field(
            instance_path, "tags", True, "{ }")
        if not isinstance(options, dict):
            raise ValueError("Invalid value for tags")
        if payload not in options.keys():
            raise ValueError("Unauthorised option")
        return payload

    def _transform_option_change(self, ev: WriterEvent) -> Optional[str]:
        payload = ev.payload
        instance_path = ev.instancePath
        options = self.evaluator.evaluate_field(
            instance_path, "options", True, """{ "a": "Option A", "b": "Option B" }""")
        if not isinstance(options, dict):
            raise ValueError("Invalid value for options")
        if payload not in options.keys():
            raise ValueError("Unauthorised option")
        return payload

    def _transform_options_change(self, ev: WriterEvent) -> Optional[List[str]]:
        payload = ev.payload
        instance_path = ev.instancePath
        options = self.evaluator.evaluate_field(
            instance_path, "options", True, """{ "a": "Option A", "b": "Option B" }""")
        if not isinstance(options, dict):
            raise ValueError("Invalid value for options")
        if not isinstance(payload, list):
            raise ValueError(
                "Invalid multiple options payload. Expected a list.")
        if not all(item in options.keys() for item in payload):
            raise ValueError("Unauthorised option")
        return payload

    def _transform_toggle(self, ev: WriterEvent) -> bool:
        payload = bool(ev.payload)
        return payload

    def _transform_keydown(self, ev) -> Dict:
        payload = ev.payload
        key = str(payload.get("key"))
        ctrl_key = bool(payload.get("ctrlKey"))
        shift_key = bool(payload.get("shiftKey"))
        meta_key = bool(payload.get("metaKey"))
        tf_payload = {
            "key": key,
            "ctrl_key": ctrl_key,
            "shift_key": shift_key,
            "meta_key": meta_key
        }
        return tf_payload

    def _transform_click(self, ev) -> Dict:
        payload = ev.payload
        ctrl_key = bool(payload.get("ctrlKey"))
        shift_key = bool(payload.get("shiftKey"))
        meta_key = bool(payload.get("metaKey"))
        tf_payload = {
            "ctrl_key": ctrl_key,
            "shift_key": shift_key,
            "meta_key": meta_key
        }
        return tf_payload

    def _transform_hashchange(self, ev) -> Dict:
        payload = ev.payload
        page_key = payload.get("pageKey")
        route_vars = dict(payload.get("routeVars"))
        tf_payload = {
            "page_key": page_key,
            "route_vars": route_vars
        }
        return tf_payload

    def _transform_page_open(self, ev) -> str:
        payload = str(ev.payload)
        return payload

    def _transform_app_open(self, ev) -> dict:
        payload = ev.payload
        page_key = payload.get("pageKey")
        route_vars = dict(payload.get("routeVars"))
        tf_payload = {
            "page_key": page_key,
            "route_vars": route_vars
        }
        return tf_payload

    def _transform_chatbot_message(self, ev) -> dict:
        payload = dict(ev.payload)
        return payload

    def _transform_chatbot_action_click(self, ev) -> str:
        payload = str(ev.payload)
        return payload

    def _transform_change(self, ev) -> str:
        payload = str(ev.payload)
        return payload

    def _transform_change_finish(self, ev) -> str:
        return self._transform_change(ev)

    def _transform_number_change(self, ev) -> Optional[float]:
        try:
            return float(ev.payload)
        except ValueError:
            return None

    def _transform_number_change_finish(self, ev) -> Optional[float]:
        return self._transform_number_change(ev)

    def _transform_webcam(self, ev) -> Any:
        return urllib.request.urlopen(ev.payload).read()

    def _file_item_transform(self, file_item: WriterFileItem) -> Dict:
        data = file_item.get("data")
        if data is None:
            raise ValueError("No data provided.")
        return {
            "name": file_item.get("name"),
            "type": file_item.get("type"),
            "data": urllib.request.urlopen(data).read()
        }

    def _transform_file_change(self, ev) -> List[Dict]:
        payload = ev.payload
        tf_payload = list(map(self._file_item_transform, payload))

        return tf_payload

    def _transform_date_change(self, ev) -> str:
        payload = ev.payload

        if not isinstance(payload, str):
            raise ValueError("Date must be a string.")
        try:
            datetime.date.fromisoformat(payload)
        except ValueError:
            raise ValueError(
                "Date must be in YYYY-MM-DD format or another valid ISO 8601 format.")

        return payload

    def _transform_time_change(self, ev) -> str:
        payload = ev.payload

        if not isinstance(payload, str):
            raise ValueError("Time must be a string.")
        try:
            time.strptime(payload, '%H:%M')
        except ValueError:
            raise ValueError(
                "Time must be in hh:mm format (in 24-hour format that includes leading zeros).")

        return payload

    def _transform_range_change(self, ev) -> list[int]:
        payload = ev.payload

        if not isinstance(payload, list):
            raise ValueError("Range must be an array.")

        if len(payload) != 2:
            raise ValueError("Range must contains exactly two values.")

        if not isinstance(payload[0], numbers.Real):
            raise ValueError("First item is not a number.")

        if not isinstance(payload[1], numbers.Real):
            raise ValueError("Second item is not a number.")

        if payload[0] > payload[1]:
            raise ValueError("First item is higher than second.")

        return payload

    def _transform_change_page_size(self, ev) -> Optional[int]:
        try:
            return int(ev.payload)
        except ValueError:
            return None

    def _transform_change_page(self, ev) -> Optional[int]:
        try:
            return int(ev.payload)
        except ValueError:
            return None


class Evaluator:

    """
    Evaluates templates and expressions in the backend.
    It allows for the sanitisation of frontend inputs.
    """

    template_regex = re.compile(r"[\\]?@{([^{]*?)}")

    def __init__(self, session_state: WriterState, session_component_tree: core_ui.ComponentTree):
        self.wf = session_state
        self.ct = session_component_tree

    def evaluate_field(self, instance_path: InstancePath, field_key: str, as_json=False, default_field_value="", base_context={}) -> Any:
        def replacer(matched):
            if matched.string[0] == "\\":  # Escaped @, don't evaluate
                return matched.string
            expr = matched.group(1).strip()
            expr_value = self.evaluate_expression(expr, instance_path, base_context)

            try:
                if as_json:
                    serialised_value = state_serialiser.serialise(expr_value)
                    if not isinstance(serialised_value, str):
                        serialised_value = json.dumps(serialised_value)
                    return serialised_value
                return expr_value
            except BaseException:
                raise ValueError(
                    f"""Couldn't serialise value of type "{ type(expr_value) }" when evaluating field "{ field_key }".""")

        component_id = instance_path[-1]["componentId"]
        component = self.ct.get_component(component_id)
        if component:
            field_value = component.content.get(field_key) or default_field_value
            replaced = None
            full_match = self.template_regex.fullmatch(field_value)

            if full_match is None:
                replaced = self.template_regex.sub(lambda m: str(replacer(m)), field_value)
            else:
                replaced = replacer(full_match)

            if (replaced is not None) and as_json:
                replaced_as_json = None
                try:
                    replaced_as_json = json.loads(replaced)
                except json.JSONDecodeError:
                    replaced_as_json = json.loads(default_field_value)
                return replaced_as_json
            else:
                return replaced
        else:
            raise ValueError(f"Couldn't acquire a component by ID '{component_id}'")


    def get_context_data(self, instance_path: InstancePath, base_context={}) -> Dict[str, Any]:
        context: Dict[str, Any] = base_context
        for i in range(len(instance_path)):
            path_item = instance_path[i]
            component_id = path_item["componentId"]
            component = self.ct.get_component(component_id)
            if not component:
                continue
            if component.type != "repeater":
                continue
            if i + 1 >= len(instance_path):
                continue
            repeater_instance_path = instance_path[0:i+1]
            next_instance_path = instance_path[0:i+2]
            instance_number = next_instance_path[-1]["instanceNumber"]
            repeater_object = self.evaluate_field(
                repeater_instance_path, "repeaterObject", True, """{ "a": { "desc": "Option A" }, "b": { "desc": "Option B" } }""")
            key_variable = self.evaluate_field(
                repeater_instance_path, "keyVariable", False, "itemId")
            value_variable = self.evaluate_field(
                repeater_instance_path, "valueVariable", False, "item")

            repeater_items: List[Tuple[Any, Any]] = []
            if isinstance(repeater_object, dict):
                repeater_items = list(repeater_object.items())
            elif isinstance(repeater_object, list):
                repeater_items = list(enumerate(repeater_object))
            else:
                raise ValueError(
                    "Cannot produce context. Repeater object must evaluate to a dictionary.")

            context[key_variable] = repeater_items[instance_number][0]
            context[value_variable] = repeater_items[instance_number][1]

        if len(instance_path) > 0:
            context['target'] = instance_path[-1]['componentId']

        return context

    def set_state(self, expr: str, instance_path: InstancePath, value: Any, base_context = {}) -> None:
        accessors = self.parse_expression(expr, instance_path, base_context)
        state_ref: StateProxy = self.wf.user_state
        leaf_state_ref: StateProxy = state_ref

        for accessor in accessors[:-1]:
            if isinstance(state_ref, StateProxy):
                leaf_state_ref = state_ref

            if isinstance(state_ref, list):
                state_ref = state_ref[int(accessor)]
            else:
                state_ref = state_ref[accessor]

        if not isinstance(state_ref, (StateProxy, dict)):
            raise ValueError(
                f"Incorrect state reference. Reference \"{expr}\" isn't part of a StateProxy or dict.")

        state_ref[accessors[-1]] = value
        leaf_state_ref.apply_mutation_marker(recursive=True)

    def parse_expression(self, expr: str, instance_path: Optional[InstancePath] = None, base_context = {}) -> List[str]:

        """ Returns a list of accessors from an expression. """

        accessors: List[str] = []
        s = ""
        level = 0

        i = 0
        while i < len(expr):
            character = expr[i]
            if character == "\\":
                if i + 1 < len(expr):
                    s += expr[i + 1]
                    i += 1
            elif character == ".":
                if level == 0:
                    accessors.append(s)
                    s = ""
                else:
                    s += character
            elif character == "[":
                if level == 0:
                    accessors.append(s)
                    s = ""
                else:
                    s += character
                level += 1
            elif character == "]":
                level -= 1
                if level == 0:
                    s = str(self.evaluate_expression(s, instance_path, base_context))
                else:
                    s += character
            else:
                s += character

            i += 1

        if s:
            accessors.append(s)

        return accessors

    def get_env_variable_value(self, expr: str):
        return os.getenv(expr[1:])

    def evaluate_expression(self, expr: str, instance_path: Optional[InstancePath] = None, base_context = {}) -> Any:
        context_data = base_context
        result = None
        if instance_path:
            context_data = self.get_context_data(instance_path, base_context)
        context_ref: Any = context_data
        state_ref: Any = self.wf.user_state.state
        accessors: List[str] = self.parse_expression(expr, instance_path, base_context)

        for accessor in accessors:
            if isinstance(state_ref, (StateProxy, dict)) and accessor in state_ref:
                state_ref = state_ref.get(accessor)
                result = state_ref
            elif isinstance(state_ref, (list)) and state_ref[int(accessor)] is not None:
                state_ref = state_ref[int(accessor)]
                result = state_ref
            elif isinstance(context_ref, dict) and accessor in context_ref:
                context_ref = context_ref.get(accessor)
                result = context_ref

        if isinstance(result, StateProxy):
            return result.to_dict()

        if result is None and expr.startswith("$"):
            return self.get_env_variable_value(expr)

        return result


class WriterSession:

    """
    Represents a session.
    """

    def __init__(self, session_id: str, cookies: Optional[Dict[str, str]], headers: Optional[Dict[str, str]]) -> None:
        self.session_id = session_id
        self.cookies = cookies
        self.headers = headers
        self.last_active_timestamp: int = int(time.time())
        new_state = WriterState.get_new()
        new_state.user_state.mutated = set()
        self.session_state = new_state
        self.session_component_tree = core_ui.build_session_component_tree(base_component_tree)
        self.event_handler = EventHandler(self)
        self.userinfo: Optional[dict] = None

    def update_last_active_timestamp(self) -> None:
        self.last_active_timestamp = int(time.time())


class SessionManager:

    """
    Stores and manages sessions.
    """

    IDLE_SESSION_MAX_SECONDS = 3600
    TOKEN_SIZE_BYTES = 32
    hex_pattern = re.compile(
        r"^[0-9a-fA-F]{" + str(TOKEN_SIZE_BYTES*2) + r"}$")

    def __init__(self) -> None:
        self.sessions: Dict[str, WriterSession] = {}
        self.verifiers: List[Callable] = []

    def add_verifier(self, verifier: Callable) -> None:
        self.verifiers.append(verifier)

    def _verify_before_new_session(self, cookies: Optional[Dict] = None, headers: Optional[Dict] = None) -> bool:
        for verifier in self.verifiers:
            args = inspect.getfullargspec(verifier).args
            arg_values = []
            for arg in args:
                if arg == "cookies":
                    arg_values.append(cookies)
                elif arg == "headers":
                    arg_values.append(headers)
            verifier_result = verifier(*arg_values)
            if verifier_result is False:
                return False
            elif verifier_result is True:
                pass
            else:
                raise ValueError(
                    "Invalid verifier return value. Must be True or False.")
        return True

    def _check_proposed_session_id(self, proposed_session_id: Optional[str]) -> bool:
        if proposed_session_id is None:
            return True
        if SessionManager.hex_pattern.match(proposed_session_id):
            return True
        return False

    def get_new_session(self, cookies: Optional[Dict] = None, headers: Optional[Dict] = None, proposed_session_id: Optional[str] = None) -> Optional[WriterSession]:
        if not self._check_proposed_session_id(proposed_session_id):
            return None
        if not self._verify_before_new_session(cookies, headers):
            return None
        new_id = None
        if proposed_session_id is None:
            new_id = self._generate_session_id()
        else:
            new_id = proposed_session_id
        new_session = WriterSession(new_id, cookies, headers)
        self.sessions[new_id] = new_session
        return new_session

    def get_session(self, session_id: Optional[str], restore_initial_mail: bool = False) -> Optional[WriterSession]:
        if session_id is None:
            return None

        session = self.sessions.get(session_id)
        if session is not None and restore_initial_mail is True:
            session.session_state.mail = copy.copy(initial_state.mail)

        return session

    def _generate_session_id(self) -> str:
        return secrets.token_hex(SessionManager.TOKEN_SIZE_BYTES)

    def clear_all(self) -> None:
        self.sessions = {}

    def close_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            return
        del self.sessions[session_id]

    def prune_sessions(self) -> None:
        cutoff_timestamp = int(time.time()) - \
            SessionManager.IDLE_SESSION_MAX_SECONDS
        prune_sessions = []
        for session_id, session in self.sessions.items():
            if session.last_active_timestamp < cutoff_timestamp:
                prune_sessions.append(session_id)
        for session_id in prune_sessions:
            self.close_session(session_id)

    @staticmethod
    def generate_session_id() -> str:
        """
        Generates a random session identifier which can be used to propose a session number before starting
        the app process in the apiinit route.
        """
        return secrets.token_hex(SessionManager.TOKEN_SIZE_BYTES)


class EventHandler:

    """
    Handles events in the context of a Session.
    """

    def __init__(self, session: WriterSession) -> None:
        self.session = session
        self.session_state = session.session_state
        self.session_component_tree = session.session_component_tree
        self.deser = EventDeserialiser(self.session_state, self.session_component_tree)
        self.evaluator = Evaluator(self.session_state, self.session_component_tree)


    def _handle_binding(self, event_type, target_component, instance_path, payload) -> None:
        if not target_component.binding:
            return
        binding = target_component.binding
        if binding["eventType"] != event_type:
            return
        self.evaluator.set_state(binding["stateRef"], instance_path, payload)

    def _get_workflow_callable(self, workflow_key: Optional[str], workflow_id: Optional[str]):
        def fn(payload, context, session):
            execution_env = {
                "payload": payload,
                "context": context,
                "session": session
            }
            if workflow_key:
                writer.workflows.run_workflow_by_key(self.session, workflow_key, execution_env)
            elif workflow_id:
                writer.workflows.run_workflow(self.session, workflow_id, execution_env)                
        return fn

    def _get_handler_callable(self, target_component: Component, event_type: str) -> Optional[Callable]:        
        if event_type == "wf-builtin-run" and Config.mode == "edit":
            return self._get_workflow_callable(None, target_component.id)
        
        if not target_component.handlers:
            return None
        handler = target_component.handlers.get(event_type)
        if not handler:
            raise ValueError(f"""Invalid handler. Couldn't find the handler for event type "{ event_type }" on component "{ target_component.id }".""")

        if handler.startswith("$runWorkflow_"):
            workflow_key = handler[13:] 
            return self._get_workflow_callable(workflow_key, None)

        current_app_process = get_app_process()
        handler_registry = current_app_process.handler_registry
        callable_handler = handler_registry.find_handler_callable(handler)
        return callable_handler


    def _call_handler_callable(
        self,
        event_type: str,
        target_component: Component,
        instance_path: List[InstancePathItem],
        payload: Any
    ) -> Any:
        
        handler_callable = self._get_handler_callable(target_component, event_type)
        if not handler_callable:
            return

        # Preparation of arguments
        context_data = self.evaluator.get_context_data(instance_path)
        context_data['event'] = event_type
        writer_args = {
            'state': self.session_state,
            'payload': payload,
            'context': context_data,
            'session':_event_handler_session_info(),
            'ui': _event_handler_ui_manager()
        }

        # Invocation of handler
        current_app_process = get_app_process()
        result = None
        captured_stdout = None
        with core_ui.use_component_tree(self.session.session_component_tree), \
            contextlib.redirect_stdout(io.StringIO()) as f:
            middlewares_executors = current_app_process.middleware_registry.executors()
            result = writer_event_handler_invoke_with_middlewares(middlewares_executors, handler_callable, writer_args)
            captured_stdout = f.getvalue()

        if captured_stdout:
            self.session_state.add_log_entry(
                "info",
                "Stdout message",
                captured_stdout
            )

        return result

    def handle(self, ev: WriterEvent) -> WriterEventResult:
        ok = True

        try:
            self.deser.transform(ev)
        except BaseException:
            ok = False
            self.session_state.add_notification(
                "error", "Error", f"A deserialisation error occurred when handling event '{ ev.type }'.")
            self.session_state.add_log_entry("error", "Deserialisation Failed",
                                             f"The data sent might be corrupt. A runtime exception was raised when deserialising event '{ ev.type }'.", traceback.format_exc())

        result = None
        try:
            instance_path = ev.instancePath
            target_id = instance_path[-1]["componentId"]
            target_component = cast(Component, self.session_component_tree.get_component(target_id))

            self._handle_binding(ev.type, target_component, instance_path, ev.payload)
            result = self._call_handler_callable(ev.type, target_component, instance_path, ev.payload)
        except BaseException:
            ok = False
            self.session_state.add_notification("error", "Runtime Error", f"An error occurred when processing event '{ ev.type }'.",
                                                )
            self.session_state.add_log_entry("error", "Runtime Exception",
                                             f"A runtime exception was raised when processing event '{ ev.type }'.", traceback.format_exc())

        return {"ok": ok, "result": result}


class DictPropertyProxy:
    """
    A descriptor based recipe that makes it possible to write shorthands
    that forward attribute access from one object onto another.

    >>> class A:
    >>>     foo: int = DictPropertyProxy("proxy_state", "prop1")
    >>>     bar: int = DictPropertyProxy("proxy_state", "prop2")
    >>>
    >>>     def __init__(self):
    >>>         self._state_proxy = StateProxy({"prop1": 1, "prop2": 2})
    >>>
    >>> a = A()
    >>> print(a.foo)

    This descriptor avoids writing the code below to establish a proxy
     with a child instance

    >>> class A:
    >>>
    >>>     def __init__(self):
    >>>         self._state_proxy = StateProxy({"prop1": 1, "prop2": 2})
    >>>
    >>>     @property
    >>>     def prop1(self):
    >>>         return self._state_proxy['prop1']
    >>>
    >>>     @foo.setter
    >>>     def prop1(self, value):
    >>>         self._state_proxy['prop1'] = value
    >>>
    """

    def __init__(self, objectName, key):
        self.objectName = objectName
        self.key = key

    def __get__(self, instance, owner=None):
        proxy = getattr(instance, self.objectName)
        return proxy[self.key]

    def __set__(self, instance, value):
        proxy = getattr(instance, self.objectName)
        proxy[self.key] = value


class DataframeRecordRemove:
    pass


class DataframeRecordProcessor():
    """
    This interface defines the signature of the methods to process the events of a
    dataframe compatible with EditableDataframe.

    A Dataframe can be any structure composed of tabular data.

    This class defines the signature of the methods to be implemented.
    """
    __metaclass__ = ABCMeta

    @staticmethod
    def match(df: Any) -> bool:
        """
        This method checks if the dataframe is compatible with the processor.
        """
        raise NotImplementedError

    @staticmethod
    def record(df: Any, record_index: int) -> dict:
        """
        This method read a record at the given line and get it back as dictionary

        >>> edf = EditableDataframe(df)
        >>> r = edf.record(1)
        """
        raise NotImplementedError

    @staticmethod
    def record_add(df: Any, payload: DataframeRecordAdded) -> Any:
        """
        signature of the methods to be implemented to process wf-dataframe-add event

        >>> edf = EditableDataframe(df)
        >>> edf.record_add({"record": {"a": 1, "b": 2}})
        """
        raise NotImplementedError

    @staticmethod
    def record_update(df: Any, payload: DataframeRecordUpdated) -> Any:
        """
        signature of the methods to be implemented to process wf-dataframe-update event

        >>> edf = EditableDataframe(df)
        >>> edf.record_update({"record_index": 12, "record": {"a": 1, "b": 2}})
        """
        raise NotImplementedError

    @staticmethod
    def record_remove(df: Any, payload: DataframeRecordRemoved) -> Any:
        """
        signature of the methods to be implemented to process wf-dataframe-action event

        >>> edf = EditableDataframe(df)
        >>> edf.record_remove({"record_index": 12})
        """
        raise NotImplementedError

    @staticmethod
    def pyarrow_table(df: Any) -> pyarrow.Table:
        """
        Serializes the dataframe into a pyarrow table
        """
        raise NotImplementedError


class PandasRecordProcessor(DataframeRecordProcessor):
    """
    PandasRecordProcessor processes records from a pandas dataframe saved into an EditableDataframe

    >>> df = pandas.DataFrame({"a": [1, 2], "b": [3, 4]})
    >>> edf = EditableDataframe(df)
    >>> edf.record_add({"a": 1, "b": 2})
    """

    @staticmethod
    @import_failure(rvalue=False)
    def match(df: Any) -> bool:
        import pandas
        return True if isinstance(df, pandas.DataFrame) else False

    @staticmethod
    def record(df: 'pandas.DataFrame', record_index: int) -> dict:
        """

        >>> edf = EditableDataframe(df)
        >>> r = edf.record(1)
        """
        import pandas

        record = df.iloc[record_index]
        if not isinstance(df.index, pandas.RangeIndex):
            index_list = df.index.tolist()
            record_index_content = index_list[record_index]
            if isinstance(record_index_content, tuple):
                for i, n in enumerate(df.index.names):
                    record[n] = record_index_content[i]
            else:
                record[df.index.names[0]] = record_index_content

        return dict(record)

    @staticmethod
    def record_add(df: 'pandas.DataFrame', payload: DataframeRecordAdded) -> 'pandas.DataFrame':
        """
        >>> edf = EditableDataframe(df)
        >>> edf.record_add({"record": {"a": 1, "b": 2}})
        """
        import pandas

        _assert_record_match_pandas_df(df, payload['record'])

        record, index = _split_record_as_pandas_record_and_index(payload['record'], df.index.names)

        if isinstance(df.index, pandas.RangeIndex):
            new_df = pandas.DataFrame([record])
            return pandas.concat([df, new_df], ignore_index=True)
        else:
            new_df = pandas.DataFrame([record], index=[index])
            return pandas.concat([df, new_df])

    @staticmethod
    def record_update(df: 'pandas.DataFrame', payload: DataframeRecordUpdated) -> 'pandas.DataFrame':
        """
        >>> edf = EditableDataframe(df)
        >>> edf.record_update({"record_index": 12, "record": {"a": 1, "b": 2}})
        """
        _assert_record_match_pandas_df(df, payload['record'])

        record: dict
        record, index = _split_record_as_pandas_record_and_index(payload['record'], df.index.names)

        record_index = payload['record_index']
        df.iloc[record_index] = record  # type: ignore

        index_list = df.index.tolist()
        index_list[record_index] = index
        df.index = index_list  # type: ignore

        return df

    @staticmethod
    def record_remove(df: 'pandas.DataFrame', payload: DataframeRecordRemoved) -> 'pandas.DataFrame':
        """
        >>> edf = EditableDataframe(df)
        >>> edf.record_remove({"record_index": 12})
        """
        record_index: int = payload['record_index']
        idx = df.index[record_index]
        df = df.drop(idx)

        return df

    @staticmethod
    def pyarrow_table(df: 'pandas.DataFrame') -> pyarrow.Table:
        """
        Serializes the dataframe into a pyarrow table
        """
        table = pyarrow.Table.from_pandas(df=df)
        return table


class PolarRecordProcessor(DataframeRecordProcessor):
    """
    PolarRecordProcessor processes records from a polar dataframe saved into an EditableDataframe

    >>> df = polars.DataFrame({"a": [1, 2], "b": [3, 4]})
    >>> edf = EditableDataframe(df)
    >>> edf.record_add({"record": {"a": 1, "b": 2}})
    """

    @staticmethod
    @import_failure(rvalue=False)
    def match(df: Any) -> bool:
        import polars
        return True if isinstance(df, polars.DataFrame) else False

    @staticmethod
    def record(df: 'polars.DataFrame', record_index: int) -> dict:
        """

        >>> edf = EditableDataframe(df)
        >>> r = edf.record(1)
        """
        record = {}
        r = df[record_index]
        for c in r.columns:
            record[c] = df[record_index, c]

        return record


    @staticmethod
    def record_add(df: 'polars.DataFrame', payload: DataframeRecordAdded) -> 'polars.DataFrame':
        _assert_record_match_polar_df(df, payload['record'])

        import polars
        new_df = polars.DataFrame([payload['record']])
        return polars.concat([df, new_df])

    @staticmethod
    def record_update(df: 'polars.DataFrame', payload: DataframeRecordUpdated) -> 'polars.DataFrame':
        # This implementation works but is not optimal.
        # I didn't find a better way to update a record in polars
        #
        # https://github.com/pola-rs/polars/issues/5973
        _assert_record_match_polar_df(df, payload['record'])

        record = payload['record']
        record_index = payload['record_index']
        for r in record:
            df[record_index, r] = record[r]

        return df

    @staticmethod
    def record_remove(df: 'polars.DataFrame', payload: DataframeRecordRemoved) -> 'polars.DataFrame':
        import polars

        record_index: int = payload['record_index']
        df_filtered = polars.concat([df[:record_index], df[record_index + 1:]])
        return df_filtered

    @staticmethod
    def pyarrow_table(df: 'polars.DataFrame') -> pyarrow.Table:
        """
        Serializes the dataframe into a pyarrow table
        """
        import pyarrow.interchange
        table: pyarrow.Table = pyarrow.interchange.from_dataframe(df)
        return table

class RecordListRecordProcessor(DataframeRecordProcessor):
    """
    RecordListRecordProcessor processes records from a list of record saved into an EditableDataframe

    >>> df = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    >>> edf = EditableDataframe(df)
    >>> edf.record_add({"record": {"a": 1, "b": 2}})
    """

    @staticmethod
    def match(df: Any) -> bool:
        return True if isinstance(df, list) else False


    @staticmethod
    def record(df: List[Dict[str, Any]], record_index: int) -> dict:
        """

        >>> edf = EditableDataframe(df)
        >>> r = edf.record(1)
        """
        r = df[record_index]
        return copy.copy(r)

    @staticmethod
    def record_add(df: List[Dict[str, Any]], payload: DataframeRecordAdded) -> List[Dict[str, Any]]:
        _assert_record_match_list_of_records(df, payload['record'])
        df.append(payload['record'])
        return df

    @staticmethod
    def record_update(df: List[Dict[str, Any]], payload: DataframeRecordUpdated) -> List[Dict[str, Any]]:
        _assert_record_match_list_of_records(df, payload['record'])

        record_index = payload['record_index']
        record = payload['record']

        df[record_index] = record
        return df

    @staticmethod
    def record_remove(df: List[Dict[str, Any]], payload: DataframeRecordRemoved) -> List[Dict[str, Any]]:
        del(df[payload['record_index']])
        return df

    @staticmethod
    def pyarrow_table(df: List[Dict[str, Any]]) -> pyarrow.Table:
        """
        Serializes the dataframe into a pyarrow table
        """
        column_names = list(df[0].keys())
        columns = {key: [record[key] for record in df] for key in column_names}

        pyarrow_columns = {key: pyarrow.array(values) for key, values in columns.items()}
        schema = pyarrow.schema([(key, pyarrow_columns[key].type) for key in pyarrow_columns])
        table = pyarrow.Table.from_arrays(
            [pyarrow_columns[key] for key in column_names],
            schema=schema
        )

        return table

class EditableDataframe(MutableValue):
    """
    Editable Dataframe makes it easier to process events from components
    that modify a dataframe like the dataframe editor.

    >>> initial_state = wf.init_state({
    >>>    "df": wf.EditableDataframe(df)
    >>> })

    Editable Dataframe is compatible with a pandas, thrillers or record list dataframe
    """
    processors = [PandasRecordProcessor, PolarRecordProcessor, RecordListRecordProcessor]

    def __init__(self, df: Union['pandas.DataFrame', 'polars.DataFrame', List[dict]]):
        super().__init__()
        self._df = df
        self.processor: Type[DataframeRecordProcessor]
        for processor in self.processors:
            if processor.match(self.df):
                self.processor = processor
                break

        if self.processor is None:
            raise ValueError("The dataframe must be a pandas, polar Dataframe or a list of record")

    @property
    def df(self) -> Union['pandas.DataFrame', 'polars.DataFrame', List[dict]]:
        return self._df

    @df.setter
    def df(self, value: Union['pandas.DataFrame', 'polars.DataFrame', List[dict]]) -> None:
        self._df = value
        self.mutate()

    def record_add(self, payload: DataframeRecordAdded) -> None:
        """
        Adds a record to the dataframe

        >>> df = pandas.DataFrame({"a": [1, 2], "b": [3, 4]})
        >>> edf = EditableDataframe(df)
        >>> edf.record_add({"record": {"a": 1, "b": 2}})
        """
        assert self.processor is not None

        self._df = self.processor.record_add(self.df, payload)
        self.mutate()

    def record_update(self, payload: DataframeRecordUpdated) -> None:
        """
        Updates a record in the dataframe

        The record must be complete otherwise an error is raised (ValueError).
        It must a value for each index / column.

        >>> df = pandas.DataFrame({"a": [1, 2], "b": [3, 4]})
        >>> edf = EditableDataframe(df)
        >>> edf.record_update({"record_index": 0, "record": {"a": 2, "b": 2}})
        """
        assert self.processor is not None

        self._df = self.processor.record_update(self.df, payload)
        self.mutate()

    def record_remove(self, payload: DataframeRecordRemoved) -> None:
        """
        Removes a record from the dataframe

        >>> df = pandas.DataFrame({"a": [1, 2], "b": [3, 4]})
        >>> edf = EditableDataframe(df)
        >>> edf.record_remove({"record_index": 0})
        """
        assert self.processor is not None

        self._df = self.processor.record_remove(self.df, payload)
        self.mutate()

    def pyarrow_table(self) -> pyarrow.Table:
        """
        Serializes the dataframe into a pyarrow table

        This mechanism is used for serializing data for transmission to the frontend.

        >>> df = pandas.DataFrame({"a": [1, 2], "b": [3, 4]})
        >>> edf = EditableDataframe(df)
        >>> pa_table = edf.pyarrow_table()
        """
        assert self.processor is not None

        pa_table = self.processor.pyarrow_table(self.df)
        return pa_table

    def record(self, record_index: int):
        """
        Retrieves a specific record in dictionary form.

        :param record_index:
        :return:
        """
        assert self.processor is not None

        record = self.processor.record(self.df, record_index)
        return record

S = TypeVar("S", bound=WriterState)

def new_initial_state(klass: Type[S], raw_state: dict) -> S:
    """
    Initializes the initial state of the application and makes it globally accessible.

    The class used for the initial state must be a subclass of WriterState.

    >>> class MyState(WriterState):
    >>>     pass
    >>>
    >>> initial_state = new_initial_state(MyState, {})
    """
    global initial_state
    if raw_state is None:
        raw_state = {}

    initial_state = klass(raw_state)

    return initial_state

"""
This variable contains the list of properties calculated for each class 
that inherits from State.

This mechanic allows Writer Framework to subscribe to mutations that trigger 
these properties when loading an application.
"""
calculated_properties_per_state_type: Dict[Type[State], List[str]] = {}

def writerproperty(path: Union[str, List[str]]):
    """
    Mechanism for declaring a calculated property whenever an attribute changes
    in the state of the Writer Framework application.

    >>> class MyState(wf.WriterState):
    >>>     counter: int
    >>>
    >>>     @wf.property("counter")
    >>>     def double_counter(self):
    >>>         return self.counter * 2

    This mechanism also supports a calculated property that depends on several dependencies.

    >>> class MyState(wf.WriterState):
    >>>     counterA: int
    >>>     counterB: int
    >>>
    >>>     @wf.property(["counterA", "counterB"])
    >>>     def counter_sum(self):
    >>>         return self.counterA + self.counterB
    """

    class Property():

        def __init__(self, func):
            self.func = func
            self.initialized = False
            self.property_name = None

        def __call__(self, *args, **kwargs):
            return self.func(*args, **kwargs)

        def __set_name__(self, owner: Type[State], name: str):
            """
            Saves the calculated properties when loading a State class.
            """
            if owner not in calculated_properties_per_state_type:
                calculated_properties_per_state_type[owner] = []

            calculated_properties_per_state_type[owner].append(name)
            self.property_name = name

        def __get__(self, instance: State, cls):
            """
            This mechanism retrieves the property instance.
            """
            args = inspect.getfullargspec(self.func)
            if len(args.args) > 1:
                logging.warning(f"Wrong signature for calculated property '{instance.__class__.__name__}:{self.property_name}'. It must declare only self argument.")
                return None

            if self.initialized is False:
                instance.calculated_property(property_name=self.property_name, path=path, handler=self.func)
                self.initialized = True

            return self.func(instance)

    return Property

def session_verifier(func: Callable) -> Callable:
    """
    Decorator for marking session verifiers.
    """

    def wrapped(*args, **kwargs):
        pass

    session_manager.add_verifier(func)
    return wrapped


def get_session() -> Optional[WriterSession]:
    """
    Retrieves the current session.

    This function works exclusively in the context of a request.
    """
    req = _current_request.get()
    if req is None:
        return None

    session_id = req.session_id
    session = session_manager.get_session(session_id)
    if not session:
        return None

    return session


def reset_base_component_tree() -> None:
    """
    Reset the base component tree to zero

    (use mainly in tests)
    """
    global base_component_tree
    base_component_tree = core_ui.build_base_component_tree()

def _clone_mutation_subscriptions(session_state: State, app_state: State, root_state: Optional['State'] = None) -> None:
    """
    clone subscriptions on mutations between the initial state of the application and the state created for the session

    >>> state = wf.init_state({"counter": 0})
    >>> state.subscribe_mutation("counter", lambda state: print(state["counter"]))

    >>> session_state = state.get_clone()

    :param session_state:
    :param app_state:
    :param root_state:
    """
    state_proxy_app = app_state._state_proxy
    state_proxy_session = session_state._state_proxy

    state_proxy_session.local_mutation_subscriptions = []

    _root_state = root_state if root_state is not None else session_state
    for mutation_subscription in state_proxy_app.local_mutation_subscriptions:
        new_mutation_subscription = copy.copy(mutation_subscription)
        new_mutation_subscription.state = _root_state if new_mutation_subscription.type == "subscription" else session_state
        session_state._state_proxy.local_mutation_subscriptions.append(new_mutation_subscription)


def parse_state_variable_expression(p: str):
    r"""
    Parses a state variable expression into a list of parts.

    >>> parse_state_variable_expression("a.b.c")
    >>> ["a", "b", "c"]

    >>> parse_state_variable_expression("a\.b.c")
    >>> ["a.b", "c"]
    """
    parts = []
    it = 0
    last_split = 0
    while it < len(p):
        if p[it] == '\\':
            it += 2
        elif p[it] == '.':
            new_part = p[last_split: it]
            parts.append(new_part.replace('\\.', '.'))

            last_split = it + 1
            it += 1
        else:
            it += 1

    new_part = p[last_split: len(p)]
    parts.append(new_part.replace('\\.', '.'))
    return parts


def writer_event_handler_build_arguments(func: Callable, writer_args: dict) -> List[Any]:
    """
    Constructs the list of arguments based on the signature of the function
    which can be a handler or middleware.

    >>> def my_event_handler(state, context):
    >>>     yield

    >>> args = writer_event_handler_build_arguments(my_event_handler, {'state': {}, 'payload': {}, 'context': {"target": '11'}, 'session': None, 'ui': None})
    >>> [{}, {"target": '11'}]

    :param func: the function that will be called
    :param writer_args: the possible arguments in writer (state, payload, ...)
    """
    handler_args = inspect.getfullargspec(func).args
    func_args = []
    for arg in handler_args:
        if arg in writer_args:
            func_args.append(writer_args[arg])

    return func_args


def writer_event_handler_invoke(callable_handler: Callable, writer_args: dict) -> Any:
    """
    Runs a handler based on its signature.

    If the handler is asynchronous, it is executed asynchronously.
    If the handler only has certain parameters, only these are passed as arguments

    >>> def my_handler(state):
    >>>     state['a'] = 2
    >>>
    >>> writer_event_handler_invoke(my_handler, {'state': {'a': 1}, 'payload': None, 'context': None, 'session': None, 'ui': None})
    """
    is_async_handler = inspect.iscoroutinefunction(callable_handler)
    if (not callable(callable_handler) and not is_async_handler):
        raise ValueError("Invalid handler. The handler isn't a callable object.")

    handler_args = writer_event_handler_build_arguments(callable_handler, writer_args)

    if is_async_handler:
        async_wrapper = _async_wrapper_internal(callable_handler, handler_args)
        result = asyncio.run(async_wrapper)
    else:
        result = callable_handler(*handler_args)

    return result

def writer_event_handler_invoke_with_middlewares(middlewares_executors: List[MiddlewareExecutor], callable_handler: Callable, writer_args: dict) -> Any:
    """
    Runs the middlewares then the handler. This function allows you to manage exceptions that are triggered in middleware

    :param middlewares_executors: The list of middleware to run
    :param callable_handler: The target handler

    >>> @wf.middleware()
    >>> def my_middleware(state, payload, context, session, ui):
    >>>     yield

    >>> executor = MiddlewareExecutor(my_middleware, {'state': {}, 'payload': None, 'context': None, 'session': None, 'ui': None})
    >>> writer_event_handler_invoke_with_middlewares([executor], my_handler, {'state': {}, 'payload': None, 'context': None, 'session': None, 'ui': None}
    """
    if len(middlewares_executors) == 0:
        return writer_event_handler_invoke(callable_handler, writer_args)
    else:
        executor = middlewares_executors[0]
        with executor.execute(writer_args):
            return writer_event_handler_invoke_with_middlewares(middlewares_executors[1:], callable_handler, writer_args)


async def _async_wrapper_internal(callable_handler: Callable, arg_values: List[Any]) -> Any:
    result = await callable_handler(*arg_values)
    return result

def _assert_record_match_pandas_df(df: 'pandas.DataFrame', record: Dict[str, Any]) -> None:
    """
    Asserts that the record matches the dataframe columns & index

    >>> _assert_record_match_pandas_df(pandas.DataFrame({"a": [1, 2], "b": [3, 4]}), {"a": 1, "b": 2})
    """
    import pandas

    columns = set(list(df.columns.values) + df.index.names) if isinstance(df.index, pandas.RangeIndex) is False else set(df.columns.values)
    columns_record = set(record.keys())
    if columns != columns_record:
        raise ValueError(f"Columns mismatch. Expected {columns}, got {columns_record}")

def _assert_record_match_polar_df(df: 'polars.DataFrame', record: Dict[str, Any]) -> None:
    """
    Asserts that the record matches the columns of polar dataframe

    >>> _assert_record_match_pandas_df(polars.DataFrame({"a": [1, 2], "b": [3, 4]}), {"a": 1, "b": 2})
    """
    columns = set(df.columns)
    columns_record = set(record.keys())
    if columns != columns_record:
        raise ValueError(f"Columns mismatch. Expected {columns}, got {columns_record}")

def _assert_record_match_list_of_records(df: List[Dict[str, Any]], record: Dict[str, Any]) -> None:
    """
    Asserts that the record matches the key in the record list (it use the first record to check)

    >>> _assert_record_match_list_of_records([{"a": 1, "b": 2}, {"a": 3, "b": 4}], {"a": 1, "b": 2})
    """
    if len(df) == 0:
        return

    columns = set(df[0].keys())
    columns_record = set(record.keys())
    if columns != columns_record:
        raise ValueError(f"Columns mismatch. Expected {columns}, got {columns_record}")

def _event_handler_session_info() -> Dict[str, Any]:
    """
    Returns the session information for the current event handler.

    This information is exposed in the session parameter of a handler
    
    """
    current_session = get_session()
    session_info: Dict[str, Any] = {}
    if current_session is not None:
        session_info['id'] = current_session.session_id
        session_info['cookies'] = current_session.cookies
        session_info['headers'] = current_session.headers
        session_info['userinfo'] = current_session.userinfo or {}

    return session_info

def _event_handler_ui_manager():
    from writer import PROPER_UI_INIT, _get_ui_runtime_error_message
    if PROPER_UI_INIT:
        from writer.ui import WriterUIManager
        return WriterUIManager()
    else:
        raise RuntimeError(_get_ui_runtime_error_message())


def _split_record_as_pandas_record_and_index(param: dict, index_columns: list) -> Tuple[dict, tuple]:
    """
    Separates a record into the record part and the index part to be able to
    create or update a row in a dataframe.

    >>> record, index = _split_record_as_pandas_record_and_index({"a": 1, "b": 2}, ["a"])
    >>> print(record) # {"b": 2}
    >>> print(index) # (1,)
    """
    final_record = {}
    final_index = []
    for key, value in param.items():
        if key in index_columns:
            final_index.append(value)
        else:
            final_record[key] = value

    return final_record, tuple(final_index)


state_serialiser = StateSerialiser()
initial_state = WriterState()
base_component_tree = core_ui.build_base_component_tree()
session_manager: SessionManager = SessionManager()
