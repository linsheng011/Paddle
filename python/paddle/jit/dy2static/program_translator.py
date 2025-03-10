# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import inspect
import textwrap
import threading
import warnings
import weakref

from paddle.fluid import _non_static_mode, core, framework
from paddle.fluid.data_feeder import check_type
from paddle.fluid.dygraph import layers
from paddle.fluid.dygraph.base import param_guard, switch_to_static_graph
from paddle.fluid.layers.utils import flatten
from paddle.utils import gast

from . import error, logging_utils
from .ast_transformer import DygraphToStaticAst
from .convert_call_func import CONVERSION_OPTIONS
from .function_spec import (
    FunctionSpec,
    _hash_spec_names,
    get_buffers,
    get_parameters,
)
from .origin_info import (
    attach_origin_info,
    create_and_update_origin_info_map,
    update_op_callstack_with_origin_info,
)
from .partial_program import partial_program_from
from .utils import (
    ALREADY_D2S,
    ast_to_func,
    ast_to_source_code,
    func_to_source_code,
    input_specs_compatible,
    make_hashable,
    type_name,
    unwrap,
)

__all__ = []

# For each traced function, we set `max_traced_program_count` = 10 to consider caching performance.
# Once exceeding the threshold, we will raise warning to users to make sure the conversion is as expected.
MAX_TRACED_PROGRAM_COUNT = 10


def synchronized(func):
    func.__lock__ = threading.Lock()

    def lock_func(*args, **kwargs):
        with func.__lock__:
            return func(*args, **kwargs)

    return lock_func


class FunctionCache:
    """
    Caches the transformed functions to avoid redundant conversions of the same function.
    """

    def __init__(self):
        # Caches the converted static functions. {dygraph_func: static_func}
        self._converted_static_func_caches = weakref.WeakKeyDictionary()
        # Caches the converted ast node for same source code. {source_code: ast_root}
        self._code_to_ast_caches = dict()
        self._dygraph_to_static = DygraphToStaticAst()

    def convert_with_cache(self, func):
        """
        Returns the cached static function or converts it when first encounters the function.
        """
        # If hit cache, return it directly.
        static_func = self._converted_static_func_caches.get(func, None)

        if static_func is None:
            static_func = self._convert(func)
            self._converted_static_func_caches[func] = static_func

        return static_func

    def _convert(self, func):
        """
        Converts dygraph function into static function. For two functions with same dedent code,
        the second function will reuse the transformed ast node of previous one.

        For example:
            # A.py
            def foo(x, y):
                z = x + y
                return z

            # B.py
            def foo(x, y):
                z = x + y
                return z

        If the conversion of A.foo happens after B.foo, it will reuse the transformed ast node of B.foo
        to speed up the conversion.
        """
        # Note: In Python2, it will raise OSError when inspect function
        # with decorator directly and function.__wrapped__ holds the actual function.
        func = unwrap(func)
        source_code = func_to_source_code(func)

        # TODO(liym27):
        #  Consider this case: source_code in self._code_to_ast_caches,
        #  but actually they are methods in different classes.
        #  Maybe use (__class__, source_code) as key
        if source_code in self._code_to_ast_caches:
            root_wrapper = self._code_to_ast_caches[source_code]
        else:
            root = gast.parse(source_code)
            root = attach_origin_info(root, func)
            root_wrapper = self._dygraph_to_static.get_static_ast(root)
            self._code_to_ast_caches[source_code] = root_wrapper

        # Get static function from AST
        static_func, file_name = ast_to_func(root_wrapper.node, func)

        create_and_update_origin_info_map(root_wrapper.node, static_func)
        return static_func

    def exist(self, func):
        return func in self._converted_static_func_caches


_CACHE_LOCK = threading.Lock()
_FUNCTION_CACHE = FunctionCache()


def convert_to_static(function):
    """
    Transforms function of dygraph into static function using the cache mechanism.

    Args:
        function(callable): The function with dygraph layers that will be converted into static layers.
    """
    if getattr(function, ALREADY_D2S, None):
        return function

    # Return directly if decorated with @not_to_static and DO NOT Cache it
    options = getattr(function, CONVERSION_OPTIONS, None)
    if options is not None and options.not_convert:
        return function.__func__ if inspect.ismethod(function) else function

    with _CACHE_LOCK:
        static_func = _FUNCTION_CACHE.convert_with_cache(function)
        setattr(static_func, ALREADY_D2S, True)
        return static_func


class CacheKey:
    """
    Cached key for ProgramCache.
    """

    __slots__ = [
        'function_spec',
        'input_args_with_spec',
        'input_kwargs_with_spec',
        'class_instance',
        'kwargs',
        '_spec_names_id',
    ]

    def __init__(
        self,
        function_spec,
        input_args_with_spec,
        input_kwargs_with_spec,
        class_instance,
        **kwargs
    ):
        """
        Initializes a cache key.

        Args:
            functions_spec(FunctionSpec): a FunctionSpec instance of decorated function.
            input_args_with_spec(list[InputSpec]): actual input args with some arguments replaced by InputSpec.
            input_kwargs_with_spec(list[{string:InputSpec}]): actual input kwargs with some arguments replaced by InputSpec.
            class_instance(object): a instance of class `Layer`.
            **kwargs(dict): manage other arguments used for better scalability
        """
        self.function_spec = function_spec
        self.input_args_with_spec = input_args_with_spec
        self.input_kwargs_with_spec = input_kwargs_with_spec
        self.class_instance = class_instance
        # NOTE: `kwargs` is usually not considered as basic member for `__hash__`
        self.kwargs = kwargs
        self._spec_names_id = _hash_spec_names(
            input_args_with_spec, input_kwargs_with_spec
        )

    @classmethod
    def from_func_and_args(cls, function_spec, args, kwargs, class_instance):
        """
        Generated a CacheKey instance by given inputs.

        Args:
            functions_spec(FunctionSpec): a FunctionSpec instance of decorated function.
            args(tuple): tuple of actual inputs arguments.
            kwargs(dict): dict of actual inputs keyword arguments.
            class_instance(object): a instance of class `Layer`.
        """
        # 1. filter `self` in args
        if args and isinstance(args[0], layers.Layer):
            args = args[1:]
        # 2. convert tensor and numpy array into InputSpec
        _args, _kwargs = function_spec.unified_args_and_kwargs(args, kwargs)
        (
            input_args_with_spec,
            input_kwargs_with_spec,
        ) = function_spec.args_to_input_spec(_args, _kwargs)

        # 3. check whether hit the cache or build a new program for the input arguments
        return CacheKey(
            function_spec,
            input_args_with_spec,
            input_kwargs_with_spec,
            class_instance,
        )

    def __hash__(self):
        error_msg = "Arguments to a `@paddle.jit.to_static` must be a hashable Python objects (or nested structures of these types)."
        with_hook = self.kwargs.get("with_hook", False)
        is_train = self.kwargs.get("is_train", False)
        return hash(
            (
                id(self.function_spec),
                make_hashable(self.input_args_with_spec, error_msg),
                make_hashable(self.input_kwargs_with_spec, error_msg),
                self._spec_names_id,
                self.class_instance,
                with_hook,
                is_train,
            )
        )

    def __eq__(self, other):
        return (type(self) is type(other)) and hash(self) == hash(other)

    def __neq__(self, other):
        return not self == other

    def __repr__(self):
        return "id(function_spec): {}, input_args_with_spec: {}, input_kwargs_with_spec: {}, class_instance: {}".format(
            id(self.function_spec),
            self.input_args_with_spec,
            self.input_kwargs_with_spec,
            self.class_instance,
        )


def unwrap_decorators(func):
    """
    Unwraps a decorated function and returns the decorator list and inner target.
    """
    decorators = []
    cur = func
    while True:
        if isinstance(cur, StaticFunction):
            decorators.append(cur)
            # Note: if `cur` is a method, keep it as bound method of class.
            instance = cur._class_instance
            if instance is not None:
                cur = cur.dygraph_function.__get__(instance)
            else:
                cur = cur.dygraph_function
        else:
            break
    return decorators, cur


class StaticFunction:
    """
    Wrapper class to Manage program conversion of decorated function.

    """

    def __init__(self, function, input_spec=None, **kwargs):
        """
        Initializes a `StaticFunction`.

        Args:
            function(callable): A function or method that will be converted into static program.
            input_spec(list[InputSpec]): list of InputSpec to specify the `shape/dtype/name` information for each input argument, default None.
            **kwargs(dict): other arguments like `build_strategy` et.al.
        """
        # save the instance `self` while decorating a method of class.

        if inspect.ismethod(function):
            self._dygraph_function = getattr(function, '__func__')
            self._class_instance = getattr(function, '__self__')

            if not hasattr(self._class_instance, '_original_funcs'):
                raise TypeError(
                    "When using 'to_static' to convert method of a class, "
                    "please ensure the class inherits from nn.Layer"
                )
            self._class_instance._original_funcs[
                function.__name__
            ] = self._dygraph_function
        else:
            self._dygraph_function = function
            self._class_instance = None

        self._input_spec = input_spec
        self._function_spec = FunctionSpec(function, input_spec)
        self._program_cache = ProgramCache()
        self._descriptor_cache = weakref.WeakKeyDictionary()
        # Note: Hold a reference to ProgramTranslator for switching `enable_to_static`.
        self._program_trans = ProgramTranslator()
        self._kwargs = kwargs
        self._training = True
        self._cuda_graph_capture_mode = ""
        self._cuda_graph_pool_id = 0

        self._property = kwargs.get("property", False)

    @property
    def is_property(self):
        # whether is class proproty to be exported.
        return self._property

    def train(self):
        if (
            isinstance(self._class_instance, layers.Layer)
            and self._class_instance.training is False
        ):
            raise RuntimeError(
                "Failed to switch train mode. {} is a Layer's method, "
                "please use Layer.train() to switch train mode.".format(
                    self.dygraph_function
                )
            )
        self._training = True

    def eval(self):
        if (
            isinstance(self._class_instance, layers.Layer)
            and self._class_instance.training is True
        ):
            raise RuntimeError(
                "Failed to switch eval mode. {} is a Layer's method, "
                "please use Layer.eval() to switch eval mode.".format(
                    self.dygraph_function
                )
            )
        self._training = False

    def __get__(self, instance, owner):
        """
        Overrides this method to parse the class instance and call bound method correctly.

        For example:

            '''
            class Net(Layer):
                def __init__(self):
                    pass

                @paddle.jit.to_static
                def forward(self, x, y):
                    return x + y

            net = Net()
            out = net(x, y)
            '''

        In above case, `net(x, y)` will call `net.forward(x, y)` firstly that is a bound method
        of `Net` instance. After decorated by `@paddle.jit.to_static`, it will firstly to call `__get__`
        to parse the class instance correctly instead of the `StaticFunction` instance.
        """
        if instance not in self._descriptor_cache:
            if instance is None:
                return self
            # Note(Aurelius84): To construct new instance of StaticFunction when we
            # first encouter the bound function of layer and cache it.
            new_static_layer = self._clone()
            new_static_layer._class_instance = instance
            self._descriptor_cache[instance] = new_static_layer

        return self._descriptor_cache[instance]

    def _clone(self):
        return self.__class__(
            self._dygraph_function, self._input_spec, **self._kwargs
        )

    def __call__(self, *args, **kwargs):
        """
        Supports to call the returned instance with input `args` and `kwargs` directly.

        Args:
            *args(tuple): tuple of all input arguments from original decorated function.
            **kwargs(dict): dict of all input keyward arguments from original decorated function.

        Return:
            Outputs of decorated function.
        """
        if self._property:
            return self._call_dygraph_function(*args, **kwargs)

        # 1. call dygraph function directly if not enable `declarative`
        if not self._program_trans.enable_to_static:
            # NOTE(liym27):
            # Here calls `warnings.warn` but not `logging_utils.warn` because by default warnings.warn(message)
            # will show up **only once**. StaticFunction.__call__ will run many times, it is appropriate to
            # display this warning message only once.
            logging_utils.warn(
                "The decorator '@paddle.jit.to_static' does NOT work when setting 'paddle.jit.enable_to_static' to False. "
                "We will just return dygraph output. If you would like to get static graph output, please call API "
                "paddle.jit.enable_to_static(True)"
            )
            return self._call_dygraph_function(*args, **kwargs)

        if not _non_static_mode():
            raise RuntimeError(
                "Failed to run the callable object {} decorated by '@paddle.jit.to_static', "
                "because it is NOT in dynamic mode. Please disable the static graph mode to enter dynamic mode with the "
                "following API: paddle.disable_static().".format(
                    self.dygraph_function
                )
            )

        # 2. trace ops from dygraph layers and cache the generated program.
        args, kwargs = self._function_spec.unified_args_and_kwargs(args, kwargs)

        try:
            concrete_program, partial_program_layer = self.get_concrete_program(
                *args, **kwargs, is_train=self._is_train_mode()
            )
            # 3. synchronize self.training attribute.
            if isinstance(self._class_instance, layers.Layer):
                partial_program_layer.training = self._class_instance.training
            else:
                partial_program_layer.training = self._training

            partial_program_layer._cuda_graph_capture_mode = (
                self._cuda_graph_capture_mode
            )
            partial_program_layer._cuda_graph_pool_id = self._cuda_graph_pool_id

            # 4. return outputs.
            try:
                return partial_program_layer(args)
            except Exception as e:
                if not hasattr(e, error.ERROR_DATA):
                    # runtime error
                    error.attach_error_data(e, in_runtime=True)
                    raise
        except Exception as e:
            error_data = getattr(e, error.ERROR_DATA, None)
            if error_data:
                error_data.raise_new_exception()
            else:
                logging_utils.warn(
                    "Please file an issue at 'https://github.com/PaddlePaddle/Paddle/issues'"
                    " if you can't handle this {} yourself.".format(type(e))
                )
                raise e

    def _is_train_mode(self):
        if self._class_instance is not None:
            if not hasattr(self._class_instance, 'training'):
                raise TypeError(
                    "When using 'to_static' to convert method of a class, "
                    "please ensure the class inherits from nn.Layer"
                )
            return self._class_instance.training
        else:
            return self._training

    def _call_dygraph_function(self, *args, **kwargs):
        """
        Calls dygraph function directly and returns the outputs.

        Args:
            *args(tuple): tuple of all input arguments from original decorated function.
            **kwargs(dict): dict of all input keyward arguments from original decorated function.

        Return:
            Outputs of dygraph function.
        """
        if self._class_instance is not None:
            dygraph_function = self._dygraph_function.__get__(
                self._class_instance
            )
        else:
            dygraph_function = self._dygraph_function

        return dygraph_function(*args, **kwargs)

    def _raise_when_property(self):
        """raise RuntimeError when property=True

        Raises:
            RuntimeError: can not call this func when property=True
        """
        if self.is_property:
            raise RuntimeError("Can not call the func when property=True.")

    def get_concrete_program(self, *args, **kwargs):
        """
        Returns traced concrete program and inner executable partial layer.

        Args:
            *args(tuple): input arguments values or InputSpec
            **kwargs(dict) : input kwargs values.

        Returns:
            Traced ConcreteProgram and executable translated Layer.
        """
        self._raise_when_property()

        with_hook = kwargs.get("with_hook", False)
        is_train = kwargs.get("is_train", True)
        if "is_train" in kwargs:
            kwargs.pop("is_train")
        if "with_hook" in kwargs:
            kwargs.pop("with_hook")
        # 1. unify args/kwargs and replace Tensor with InputSpec
        if len(args) != len(self._function_spec.args_name):
            args, kwargs = self._function_spec.unified_args_and_kwargs(
                args, kwargs
            )
        (
            input_args_with_spec,
            input_kwargs_with_spec,
        ) = self._function_spec.args_to_input_spec(args, kwargs)

        # 2. generate cache key
        cache_key = CacheKey(
            self._function_spec,
            input_args_with_spec,
            input_kwargs_with_spec,
            self._class_instance,
            **self._kwargs,
            with_hook=with_hook,
            is_train=is_train
        )

        # 3. check whether hit the cache or build a new program for the input arguments
        concrete_program, partial_program_layer = self._program_cache[cache_key]
        return concrete_program, partial_program_layer

    def get_traced_count(self):
        """
        Returns the number of traced programs for the decorated function.
        """
        return len(self._program_cache)

    @property
    def code(self):
        """
        Returns the source code of transformed static function for debugging.
        """
        static_func = convert_to_static(self._dygraph_function)
        source_code = func_to_source_code(static_func)
        return source_code

    @property
    def dygraph_function(self):
        """
        Returns the original decorated function.
        """
        return self._dygraph_function

    @property
    def concrete_program(self):
        """
        Returns recent ConcreteProgram instance of decorated function.

        Examples:
            .. code-block:: python

                import paddle
                from paddle.jit import to_static
                from paddle.static import InputSpec

                paddle.disable_static()

                def foo(x, y):
                    z = x + y
                    return z

                # usage 1:
                decorated_foo = to_static(foo, input_spec=[InputSpec([10], name='x'), InputSpec([10], name='y')])
                print(decorated_foo.concrete_program)

                # usage 2:
                decorated_foo = to_static(foo)
                out_foo = decorated_foo(paddle.rand([10]), paddle.rand([10]))
                print(decorated_foo.concrete_program)
        """
        return self.concrete_program_specify_input_spec(input_spec=None)

    def concrete_program_specify_input_spec(
        self, input_spec=None, with_hook=False
    ):
        """
        Returns recent ConcreteProgram instance of decorated function while
        specifying input_spec. If the self._function_spec already has
        input_spec, it will check the compatibility of input input_spec and
        the self._function_spec.input_spec. If input input_spec=None, then
        this method uses self._function_spec.input_spec

        args:
            input_spec (list[InputSpec], optional): Describes the input of
                the translate function.
        """
        self._raise_when_property()
        # if specific the `input_spec`, the length of program_cache will always 1,
        # else, return the last one.
        cached_program_len = len(self._program_cache)
        # If specific `input_spec`, apply convertion from dygraph layers into static Program.
        if cached_program_len == 0:
            desired_input_spec = input_spec
            if self._function_spec.input_spec is not None:
                if input_spec is not None and not input_specs_compatible(
                    flatten(input_spec), flatten(self._function_spec.input_spec)
                ):
                    raise ValueError(
                        "The `input_spec`: {} used to construct concrete_program is conflict with the `input_spec`: {} in `@paddle.jit.to_static`".format(
                            input_spec, self._function_spec.input_spec
                        )
                    )
                # NOTE(chenweihang): we should always translated program based on the `input_spec`
                # decorated on forward if it is valid
                desired_input_spec = self._function_spec.input_spec
                if input_spec is not None:
                    logging_utils.warn(
                        "\n\nYou have specified `input_spec` both in function definition (higher priority) and `paddle.jit.save` (will be ignored.)\n\n\t Using: {}\n\n\t Ignore: {}\n".format(
                            desired_input_spec, input_spec
                        )
                    )

            has_input_spec = desired_input_spec is not None
            if has_input_spec:
                concrete_program, _ = self.get_concrete_program(
                    *desired_input_spec,
                    with_hook=with_hook,
                    is_train=self._is_train_mode()
                )
                return concrete_program
            else:
                raise ValueError(
                    "No valid transformed program for {}.\n\t    Please specific `input_spec` in `@paddle.jit.to_static` or feed input tensor to call the decorated function at once.\n".format(
                        self._function_spec
                    )
                )
        elif with_hook:
            cache_key = self._program_cache._recent_cache_key
            cache_key.kwargs["with_hook"] = True
            concrete_program, _ = self._program_cache[cache_key]
            return concrete_program

        # If more than one programs have been cached, return the recent converted program by default.
        elif cached_program_len > 1:
            logging_utils.warn(
                "Current {} has more than one cached programs: {}, the last traced progam will be return by default.".format(
                    self._function_spec, cached_program_len
                )
            )

        cache_key, (
            concrete_program,
            partial_layer,
        ) = self._program_cache.last()
        return concrete_program

    def rollback(self):
        """
        Rollback into original dygraph functions for current class instance.

        Returns:
            Function or Method

        Example::
            .. code-block:: python

                import paddle

                class Net(paddle.nn.Layer):
                    def __init__(self):
                        super().__init__()

                    def forward(self, x, flag=True):
                        if flag:
                            out = x + 1
                        else:
                            out = x - 1
                        return out

                x = paddle.randn([10, 1], 'float32')
                net = paddle.jit.to_static(Net())  # convert into static graph mode
                out = net(x)

                net.forward.rollback()  # rollback into dygraph mode
                out = net(x)
        """

        def rollback_impl(class_instance):
            for name, func in class_instance._original_funcs.items():
                setattr(class_instance, name, func.__get__(class_instance))

            for sublayer in class_instance.sublayers(include_self=False):
                rollback_impl(sublayer)

        if self._class_instance is None:
            return self._dygraph_function

        # only rollback sub-functions on path of top _dygraph_function
        func_name = self._dygraph_function.__name__
        assert (
            func_name in self._class_instance._original_funcs
        ), "Not Found function '{}' in class '{}'.".format(
            func_name, self._class_instance.__name__
        )
        func = self._class_instance._original_funcs[func_name]
        setattr(
            self._class_instance, func_name, func.__get__(self._class_instance)
        )

        for sublayer in self._class_instance.sublayers(include_self=False):
            rollback_impl(sublayer)

        return getattr(self._class_instance, func_name)

    def __deepcopy__(self, memo):
        """
        Customized behavior for copy.deepcopy, return original decorated function instead
        of a new StaticFunction Object. StaticFunction itself is not copyable becuase it's
        associated with class_instance.

        We add __deepcopy__ here only for the following usage:

        Example::
            .. code-block:: python

                import copy
                import paddle

                class Net(paddle.nn.Layer):
                    def __init__(self):
                        super().__init__()

                    def forward(self, x, flag=True):
                        if flag:
                            out = x + 1
                        else:
                            out = x - 1
                        return out

                x = paddle.randn([10, 1], 'float32')
                net = paddle.jit.to_static(Net())  # convert into static graph mode

                copy_net = copy.deepcopy(net)      # deepcopy a new net without @to_static

        Please attention that original 'net' will unwrap @to_static and rollback into simple Layer.
        """
        if self._class_instance is not None:
            net_name = type(self._class_instance).__name__
            logging_utils.log(
                level=-1,
                msg="Not recommend to deepcopy '{}' decorated with @to_static, it has side effect that will"
                " rollback into original state before @to_static. Please deepcopy '{}' before applying @to_static.".format(
                    net_name, net_name
                ),
            )
            self.rollback()
            return self._dygraph_function.__get__(
                memo[id(self._class_instance)]
            )
        else:
            return self._dygraph_function

    @property
    def inputs(self):
        """
        Returns input tensors of recent converted static program.
        """
        self._raise_when_property()
        concrete_program = self.concrete_program
        inputs = [
            var
            for var in flatten(concrete_program.inputs)
            if isinstance(var, framework.Variable)
        ]
        return inputs

    @property
    def outputs(self):
        """
        Returns output tensors of recent converted static program.
        """
        self._raise_when_property()
        concrete_program = self.concrete_program
        outputs = [
            var
            for var in flatten(concrete_program.outputs)
            if isinstance(var, framework.Variable)
        ]

        return outputs

    @property
    def main_program(self):
        """
        Returns recent converted static main program.
        """
        self._raise_when_property()
        concrete_program = self.concrete_program
        main_program = concrete_program.main_program
        return main_program

    @property
    def program_cache(self):
        return self._program_cache

    @property
    def function_spec(self):
        return self._function_spec


def _verify_init_in_dynamic_mode(class_instance):
    """
    Verifies the instance is initialized in dynamic mode.
    """
    if isinstance(class_instance, layers.Layer):
        if not class_instance._init_in_dynamic_mode:
            raise RuntimeError(
                " `paddle.jit.to_static` is only available in dynamic mode. Please call `paddle.disable_static()` before "
                "initializing your Layer class `{}` . Because parameters of Layer class should be initialized firstly "
                "in dynamic mode while applying transformation.".format(
                    class_instance
                )
            )


class HookHelper:
    """
    Only For converting pre/post hooks operation in outermost layer while jit.save.
    Because hooks in sublayer have been processed automatically.
    """

    def __init__(self, func, class_instance, with_hook=False):
        self.func = func
        self.class_instance = class_instance
        self.with_hook = with_hook
        self.need_apply_hook = (
            with_hook
            and isinstance(self.class_instance, layers.Layer)
            and getattr(func, "__name__") == "forward"
        )

    def apply_pre_hooks(self, inputs):
        """
        Apply _forward_pre_hooks from outermost layer
        """
        if not self.need_apply_hook:
            return inputs

        inputs = inputs[1:]
        for forward_pre_hook in self.class_instance._forward_pre_hooks.values():
            hook_result = forward_pre_hook(self.class_instance, inputs)
            if hook_result is not None:
                if not isinstance(hook_result, tuple):
                    hook_result = (hook_result,)
                inputs = hook_result

        return [self.class_instance] + list(inputs)

    def apply_post_hooks(self, inputs, outputs):
        """
        Apply _forward_post_hooks from outermost layer
        """
        if not self.need_apply_hook:
            return outputs

        inputs = inputs[1:]
        for (
            forward_post_hook
        ) in self.class_instance._forward_post_hooks.values():
            hook_result = forward_post_hook(
                self.class_instance, inputs, outputs
            )
            if hook_result is not None:
                outputs = hook_result

        inputs.insert(0, self.class_instance)
        return outputs


class ConcreteProgram:

    __slots__ = [
        'inputs',
        'outputs',
        'main_program',
        "startup_program",
        "parameters",
        "function",
        'kwargs',
    ]

    def __init__(
        self,
        inputs,
        outputs,
        parameters,
        function,
        main_program,
        startup_program=None,
        **kwargs
    ):
        self.inputs = inputs
        self.outputs = outputs
        self.main_program = main_program
        self.startup_program = startup_program
        self.parameters = parameters
        self.function = function
        self.kwargs = kwargs

    @switch_to_static_graph
    def _to_prim(self):
        # TODO(Aurelius84): Fix this cycle import problem
        from paddle.incubate.autograd.primapi import to_prim

        to_prim(self.main_program.blocks)

    @staticmethod
    @switch_to_static_graph
    def from_func_spec(
        func_spec, input_spec, input_kwargs_spec, class_instance, **kwargs
    ):
        """
        Builds the main_program with specialized inputs and returns outputs
        of program as fetch_list.

        Args:
            func_spec(FunctionSpec): A FunctionSpec instance for decorated function.
            input_spec(list[InputSpec]):
        """
        # verify the instance is initialized in imperative mode.
        _verify_init_in_dynamic_mode(class_instance)

        # Transforms dygraph function into static function and caches it.
        dygraph_function = func_spec.dygraph_function
        static_func = convert_to_static(dygraph_function)
        # apply pre\post hook for outermost layer
        hook_helper = HookHelper(
            dygraph_function, class_instance, kwargs.get("with_hook", False)
        )

        main_program, startup_program = framework.Program(), framework.Program()
        # Note: The random seed should be synchronized into cached program
        # if set in `fluid.dygraph_guard` because some ops rely on it, such as
        # `fluid.layers.dropout`.
        main_program.random_seed = framework.default_main_program().random_seed
        startup_program.random_seed = (
            framework.default_startup_program().random_seed
        )

        from paddle.fluid.dygraph.base import _switch_declarative_mode_guard_

        with framework.program_guard(main_program, startup_program):
            with _switch_declarative_mode_guard_(is_declarative=True):
                # 1. Adds `fluid.data` layers for input if needed
                static_inputs = func_spec.to_static_inputs_with_spec(
                    input_spec, main_program
                )
                _kwargs = func_spec.to_static_inputs_with_spec(
                    input_kwargs_spec, main_program
                )
                if class_instance:
                    static_inputs = tuple(
                        [class_instance] + list(static_inputs)
                    )

                # 2. Builds program only once and returns the output Variables.
                with param_guard(
                    get_parameters(class_instance, False)
                ), param_guard(get_buffers(class_instance, False)):
                    try:
                        # only for jit.save, do nothing while train and eval process
                        inputs = hook_helper.apply_pre_hooks(static_inputs)
                        if _kwargs:
                            outputs = static_func(*inputs, **_kwargs)
                        else:
                            outputs = static_func(*inputs)
                        outputs = hook_helper.apply_post_hooks(inputs, outputs)
                    except BaseException as e:
                        # NOTE: If e is raised in compile time, e should be attached to ERROR_DATA here.
                        error.attach_error_data(e)
                        error_data = getattr(e, error.ERROR_DATA, None)
                        if error_data:
                            error_data.raise_new_exception()
                        raise

                from paddle.jit.dy2static.program_translator import (
                    ProgramTranslator,
                )

                # 3. Gets all ParamBases and buffered VarBases in the function
                all_parameters_and_buffers = (
                    ProgramTranslator.get_instance()._params_recorder.pop(
                        main_program
                    )
                )

                if outputs is not None:
                    need_wrap_into_list = (
                        not isinstance(outputs, (tuple, list))
                        or len(outputs) == 1
                    )
                    if need_wrap_into_list:
                        outputs = [outputs]

        main_program = update_op_callstack_with_origin_info(main_program)

        return ConcreteProgram(
            inputs=static_inputs,
            outputs=outputs,
            parameters=all_parameters_and_buffers,
            function=dygraph_function,
            main_program=main_program,
            startup_program=startup_program,
            **kwargs
        )


def _extract_indeed_params_buffers(class_instance):
    """
    To filter not initialzed buffers.
    """
    params = list(get_parameters(class_instance).values())
    buffers = list(get_buffers(class_instance).values())
    buffers = [buffer for buffer in buffers if len(buffer.shape) != 0]

    return params + buffers


class ParametersRecorder:
    def __init__(self):
        self.params_dict = {}

    @synchronized
    def add(self, program, param):
        """use the default_program as key, append param the parameter list."""
        key = self._program_hash(program)
        if key not in self.params_dict:
            self.params_dict[key] = set()
        params = self.params_dict[key]
        params.add(param)

    def pop(self, program):
        params = self.params_dict.get(self._program_hash(program))
        if params is None:
            return []
        del self.params_dict[self._program_hash(program)]
        return list(params)

    def _program_hash(self, program):
        """
        because program is not deleted while calling from_func_spec.
        so it's ok to use id(program)
        """
        return id(program)


class FallbackProgramLayer(object):
    __slots__ = [
        '_instance',
        '_dy_func',
        'training',
        '_cuda_graph_capture_mode',
        '_cuda_graph_pool_id',
    ]

    def __init__(self, instance, dy_func):
        self._instance = instance
        self._dy_func = dy_func

    def __call__(self, inputs):
        return self._dy_func(*inputs)

    def __getattr__(self, key):
        if key not in self.__slots__:
            raise RuntimeError(
                "There raises a exception after applying `@paddle.jit.to_static()` and already switch into fallback mode. \n"
                "You can't get attribute for a fallback program layer. Please check `to_static.error` file for detail."
            )
        elif key in ['training']:
            if self._instance is not None:
                return getattr(self._instance, key)
            return

        return super().__getattr__(key)

    def __setattr__(self, key, value):
        if key not in self.__slots__:
            raise RuntimeError(
                "There raises a exception after applying `@paddle.jit.to_static()` and already switch into fallback mode. \n"
                "You can't get attribute for a fallback program layer. Please check `to_static.error` file for detail."
            )
        elif key in ['training']:
            if self._instance is not None:
                return setattr(self._instance, key, value)
            return

        return super().__setattr__(key, value)


class ProgramCache:
    """
    Wrapper class for the program functions defined by dygraph function.
    """

    dy2static_error_file = "to_static.error"

    def __init__(self):
        # {hash_id : (concrete_program, partial_layer)}
        self._caches = collections.OrderedDict()
        # trace mostly recent used program
        self._recent_key = None
        self._recent_cache_key = None

    def _build_once(self, cache_key):
        # TODO(Aurelius84): Need a gloabl FLAGS to enable/disable to_prim
        enable_prim = cache_key.kwargs['build_strategy'].build_cinn_pass
        # NOTE(xiongkun): Need a global FLAGS to enable/disable fallback
        enable_fallback = enable_prim
        # TODO(CZ): later when use cinn, set_prim_all_enabled and check_and_set_prim_all_enabled will be set at else branch.
        core.check_and_set_prim_all_enabled()
        try:
            concrete_program = ConcreteProgram.from_func_spec(
                func_spec=cache_key.function_spec,
                input_spec=cache_key.input_args_with_spec,
                input_kwargs_spec=cache_key.input_kwargs_with_spec,
                class_instance=cache_key.class_instance,
                **cache_key.kwargs
            )
        except Exception as e:
            if enable_fallback:
                warnings.warn(
                    "Exception is thrown while applying @paddle.jit.to_static. It will fallback into dygraph mode for training.\n"
                    "1. You can check `to_static.error` file in current workspace directory for detail.\n"
                    "2. In fallback mode, you can only do training, can't call paddle.jit.save(). Please modify model code according `to_static.error` firstly"
                )
                # TODO(xiongkun) change different file name to avoid overwrite.
                with open(self.dy2static_error_file, "w") as fp:
                    fp.write(str(e))

                fallback_layer = FallbackProgramLayer(
                    cache_key.class_instance,
                    cache_key.function_spec.dygraph_function,
                )
                return fallback_layer, fallback_layer
            else:
                raise

        concrete_program._to_prim()
        return concrete_program, partial_program_from(concrete_program)

    def __getitem__(self, item):
        if not isinstance(item, CacheKey):
            raise ValueError(
                'type(item) should be CacheKey, but received %s'
                % type_name(item)
            )
        item_id = hash(item)
        self._recent_cache_key = item
        self._recent_key = item_id
        if item_id not in self._caches:
            self._caches[item_id] = self._build_once(item)
            # Note: raise warnings if number of traced program is more than `max_tracing_count`
            current_tracing_count = len(self._caches)
            if current_tracing_count > MAX_TRACED_PROGRAM_COUNT:
                logging_utils.warn(
                    "Current traced program number: {} > `max_tracing_count`:{}. Too much cached programs will bring expensive overhead. "
                    "The reason may be: (1) passing tensors with different shapes, (2) passing python objects instead of tensors.".format(
                        current_tracing_count, MAX_TRACED_PROGRAM_COUNT
                    )
                )

        return self._caches[item_id]

    def get_program(self, item):
        if not isinstance(item, CacheKey):
            raise ValueError(
                "Input item's type should be FunctionSpec, but received %s"
                % type_name(item)
            )
        item_id = hash(item)
        if item_id not in self._caches:
            raise RuntimeError(
                "Failed to find program for input item, please decorate input function by `@paddle.jit.to_static`."
            )
        return self._caches[item_id]

    def last(self):
        assert (
            len(self._caches) >= 1
        ), "No valid cached program in ProgramCache."
        assert self._recent_key is not None
        return self._recent_key, self._caches[self._recent_key]

    def __len__(self):
        return len(self._caches)

    def concrete_programs(self):
        return [cp for key, (cp, _) in self._caches.items()]


class ProgramTranslator:
    """
    Class to translate dygraph function into static graph function. The object
    of this class is a singleton.

    Args:
        None.

    Returns:
        ProgramTranslator: the singleton object.

    Examples:
        .. code-block:: python

            import paddle

            # Two methods get same object because ProgramTranslator is a singleton
            paddle.jit.ProgramTranslator()
            paddle.jit.ProgramTranslator.get_instance()

    """

    _singleton_lock = threading.Lock()
    _instance = None

    @synchronized
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = object.__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        if cls._instance is not None:
            cls._instance._initialized = False
            cls._instance.__init__()

    def __init__(self):
        # To make sure that calls __init__ only once.
        if self._initialized:
            return
        self._initialized = True
        self._program_cache = ProgramCache()
        self._params_recorder = ParametersRecorder()
        self.enable_to_static = True

    def enable(self, enable_to_static):
        """
        Enable or disable the converting from imperative to static graph by
        ProgramTranslator globally.

        Args:
            enable_to_static (bool): True or False to enable or disable converting to static.

        Returns:
            None.

        Examples:
            .. code-block:: python

                import paddle


                @paddle.jit.to_static
                def func(x):
                    if paddle.mean(x) > 0:
                        x_v = x - 1
                    else:
                        x_v = x + 1
                    return x_v


                paddle.jit.enable_to_static(False)

                x = paddle.ones([1, 2])
                # ProgramTranslator is disabled so the func is run in dygraph
                print(func(x))  # [[0. 0.]]

        """
        check_type(
            enable_to_static,
            "enable_to_static",
            bool,
            "ProgramTranslator.enable",
        )
        self.enable_to_static = enable_to_static

    def get_output(self, dygraph_func, *args, **kwargs):
        """
        Returns the output dygraph Tensor for dygraph function. The dygraph
        function will be translated into static graph function so the under
        beneath numerical result will be calculated by static graph mode.

        Args:
            dygraph_func (callable): the dygraph function.
            *args (tuple): the input argument of dygraph_func.
            **kwargs (dict): the input argument of dygraph_func.

        Returns:
            Tensor or tuple of Tensors: the dygraph Tensor containing digital result.

        Examples:
            .. code-block:: python

                import paddle


                def func(x):
                    if paddle.mean(x) > 0:
                        x_v = x - 1
                    else:
                        x_v = x + 1
                    return x_v


                prog_trans = paddle.jit.ProgramTranslator()

                x = paddle.ones([1, 2])
                x_v = prog_trans.get_output(func, x)
                print(x_v)  # [[0. 0.]]

        """
        assert callable(
            dygraph_func
        ), "Input dygraph_func is not a callable in ProgramTranslator.get_output"

        if not self.enable_to_static:
            # Here calls `warnings.warn` but not `logging_utils.warn` because by default warnings.warn(message)
            # will show up **only once**.
            logging_utils.warn(
                "The ProgramTranslator.get_output doesn't work when setting ProgramTranslator.enable to False. "
                "We will just return dygraph output. "
                "Please call ProgramTranslator.enable(True) if you would like to get static output."
            )
            return dygraph_func(*args, **kwargs)
        try:
            function_spec = FunctionSpec(dygraph_func)
            cache_key = CacheKey.from_func_and_args(
                function_spec,
                args,
                kwargs,
                getattr(dygraph_func, '__self__', None),
            )
            _, partial_program_layer = self._program_cache[cache_key]

            if args and isinstance(args[0], layers.Layer):
                # Synchronize self.training attribute.
                partial_program_layer.training = args[0].training
                args = args[1:]
            try:
                return partial_program_layer(args)
            except BaseException as e:
                # NOTE:
                # 1. If e is raised in compile time, e should have been attached to ERROR_DATA before;
                # 2. If e raised in runtime, e should be attached to ERROR_DATA here.
                if not hasattr(e, error.ERROR_DATA):
                    # runtime error
                    error.attach_error_data(e, in_runtime=True)
                raise
        except BaseException as e:
            error_data = getattr(e, error.ERROR_DATA, None)
            if error_data:
                error_data.raise_new_exception()
            else:
                logging_utils.warn(
                    "Please file an issue at 'https://github.com/PaddlePaddle/Paddle/issues'"
                    " if you can't handle this {} yourself.".format(type(e))
                )
                raise e

    def get_func(self, dygraph_func):
        """
        Returns a callable function which converts imperative dygraph APIs of
        the input dygraph_func into declarative net-building APIs, which means
        it doesn't return immediate digital result as get_output does.
        Users should handle Program and Executor by themselves.

        Args:
            dygraph_func (callable): the dygraph function.

        Returns:
            callable: converting imperative dygraph APIs into declarative
            net-building APIs.

        Examples:
            .. code-block:: python

                import paddle


                def func(x):
                    if paddle.mean(x) > 0:
                        x_v = x - 1
                    else:
                        x_v = x + 1
                    return x_v


                prog_trans = paddle.jit.ProgramTranslator()
                static_func = prog_trans.get_func(func)
                print(callable(static_func)) # True

        """
        assert callable(
            dygraph_func
        ), "Input dygraph_func is not a callable in ProgramTranslator.get_func"

        if not self.enable_to_static:
            logging_utils.warn(
                "The ProgramTranslator.get_func doesn't work when setting ProgramTranslator.enable to False. We will "
                "just return dygraph output. Please call ProgramTranslator.enable(True) if you would like to get static output."
            )
            return dygraph_func

        static_func = convert_to_static(dygraph_func)
        return static_func

    def get_program(self, dygraph_func, *args, **kwargs):
        """
        Returns the translated static program and input/output Tensors from
        dygraph function. The users can use the program to run by executor.

        Args:
            dygraph_func (callable): the dygraph function.
            *args (tuple): the input argument of dygraph_func.
            **kwargs (dict): the input argument of dygraph_func.

        Returns:
            tuple of (main_program, startup_program, inputs, outputs) whose
            types are (Program, Program, list of Tensors, list of Tensors).
            main_program: the converted main program.
            startup_program: the converted startup program.
            inputs: list of input Tensors which need to be fed.
            outputs: list of output Tensors which users can fetch.

        Examples:
            .. code-block:: python

                import paddle


                def func(x):
                    if paddle.mean(x) > 0:
                        x_v = x - 1
                    else:
                        x_v = x + 1
                    return x_v


                prog_trans = paddle.jit.ProgramTranslator()
                x = paddle.ones([1, 2])
                main_prog, start_prog, inputs, outputs = prog_trans.get_program(func, x)
                print([i.name for i in inputs])
                # [u'generated_tensor_0'] the feed input Tensor name representing x
                print([o.name for o in outputs])
                # [u'_generated_var_4'] the fetch output Tensor name representing x_v

        """
        assert callable(
            dygraph_func
        ), "Input dygraph_func is not a callable in ProgramTranslator.get_program"

        if not self.enable_to_static:
            logging_utils.warn(
                "The ProgramTranslator.get_program doesn't work when setting ProgramTranslator.enable to False."
                "We will just return dygraph output. "
                "Please call ProgramTranslator.enable(True) if you would like to get static output."
            )
            return dygraph_func(*args, **kwargs)

        function_spec = FunctionSpec(dygraph_func)
        cache_key = CacheKey.from_func_and_args(
            function_spec, args, kwargs, getattr(dygraph_func, '__self__', None)
        )
        concrete_program, partial_program_layer = self._program_cache[cache_key]

        # Note: concrete_program hold all input/output infos include non-Variable
        input_vars = [
            var
            for var in concrete_program.inputs
            if isinstance(var, framework.Variable)
        ]
        output_vars = [
            var
            for var in concrete_program.outputs
            if isinstance(var, framework.Variable)
        ]

        return (
            concrete_program.main_program,
            concrete_program.startup_program,
            input_vars,
            output_vars,
        )

    def get_code(self, dygraph_func):
        """
        Returns the translated static function string code from dygraph function.

        Args:
            dygraph_func (callable): the dygraph function.

        Returns:
            str: the string code of translated static function.

        Examples:
            .. code-block:: python

                import paddle


                def func(x):
                    if paddle.mean(x) > 0:
                        x_v = x - 1
                    else:
                        x_v = x + 1
                    return x_v


                prog_trans = paddle.jit.ProgramTranslator()

                code = prog_trans.get_code(func)
                print(type(code)) # <class 'str'>

        """
        assert callable(
            dygraph_func
        ), "Input dygraph_func is not a callable in ProgramTranslator.get_code"
        # Gets AST from dygraph function

        unwrap_func = unwrap(dygraph_func)
        raw_code = inspect.getsource(unwrap_func)
        code = textwrap.dedent(raw_code)
        root = gast.parse(code)

        # Transform AST
        dygraph_to_static = DygraphToStaticAst()
        root_wrapper = dygraph_to_static.get_static_ast(root)

        # Get source_code
        source_code = ast_to_source_code(root_wrapper.node)
        return source_code

    def get_program_cache(self):
        """
        Returns the ProgramCache instance. This method is used by PaddlePaddle
        developers to manage program cache in ProgramTranslator. Normal users
        don't have to call this method.

        Returns:
            ProgramCache: ProgramCache instance of ProgramTranslator.

        Examples:
            .. code-block:: python

                import paddle

                prog_trans = paddle.jit.ProgramTranslator()
                prog_cache = prog_trans.get_program_cache()

        """
        return self._program_cache


def enable_to_static(enable_to_static_bool):

    """
    Enable or disable the converting from imperative to static graph by
    ProgramTranslator globally.

    Args:
        enable_to_static_bool (bool): True or False to enable or disable converting to static.

    Returns:
        None.

    Examples:
        .. code-block:: python

            import paddle


            @paddle.jit.to_static
            def func(x):
                if paddle.mean(x) > 0:
                    x_v = x - 1
                else:
                    x_v = x + 1
                return x_v


            paddle.jit.enable_to_static(False)

            x = paddle.ones([1, 2])
            # ProgramTranslator is disabled so the func is run in dygraph
            print(func(x))  # [[0. 0.]]

    """
    check_type(
        enable_to_static_bool,
        "enable_to_static_bool",
        bool,
        "paddle.jit.enable_to_static",
    )
    _program_trans = ProgramTranslator()
    _program_trans.enable(enable_to_static_bool)
