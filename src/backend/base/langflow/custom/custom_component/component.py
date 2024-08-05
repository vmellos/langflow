import inspect
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional, Union, get_type_hints
from uuid import UUID

import nanoid  # type: ignore
import yaml
from pydantic import BaseModel

from langflow.helpers.custom import format_type
from langflow.schema.artifact import get_artifact_type, post_process_raw
from langflow.schema.data import Data
from langflow.schema.message import Message
from langflow.services.tracing.schema import Log
from langflow.template.field.base import UNDEFINED, Input, Output
from langflow.template.frontend_node.custom_components import ComponentFrontendNode
from langflow.utils.async_helpers import run_until_complete

from .custom_component import CustomComponent

if TYPE_CHECKING:
    from langflow.graph.edge.schema import EdgeData
    from langflow.graph.vertex.base import Vertex
    from langflow.inputs.inputs import InputTypes

BACKWARDS_COMPATIBLE_ATTRIBUTES = ["user_id", "vertex", "tracing_service"]


class Component(CustomComponent):
    inputs: List["InputTypes"] = []
    outputs: List[Output] = []
    code_class_base_inheritance: ClassVar[str] = "Component"
    _output_logs: dict[str, Log] = {}

    def __init__(self, **kwargs):
        # if key starts with _ it is a config
        # else it is an input
        inputs = {}
        config = {}
        for key, value in kwargs.items():
            if key.startswith("_"):
                config[key] = value
            else:
                inputs[key] = value
        self._inputs: dict[str, "InputTypes"] = {}
        self._outputs: dict[str, Output] = {}
        self._results: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._parameters = inputs or {}
        self._edges: list[EdgeData] = []
        self._components: list[Component] = []
        self._call_inputs: dict[str, Any] = {}
        self.set_attributes(self._parameters)
        self._output_logs = {}
        config = config or {}
        if "_id" not in config:
            config |= {"_id": f"{self.__class__.__name__}-{nanoid.generate(size=5)}"}
        super().__init__(**config)
        if hasattr(self, "_trace_type"):
            self.trace_type = self._trace_type
        if not hasattr(self, "trace_type"):
            self.trace_type = "chain"
        if self.inputs is not None:
            self.map_inputs(self.inputs)
        if self.outputs is not None:
            self.map_outputs(self.outputs)
        # Set output types
        self._set_output_types()
        self.set_class_code()

    def _set_call_inputs(self, key: str, value: Any):
        self._call_inputs[key] = value

    def set_class_code(self):
        # Get the source code of the calling class
        if self._code:
            return
        try:
            module = inspect.getmodule(self.__class__)
            if module is None:
                raise ValueError("Could not find module for class")
            class_code = inspect.getsource(module)
            self._code = class_code
        except OSError:
            raise ValueError(f"Could not find source code for {self.__class__.__name__}")

    def set(self, **kwargs):
        """
        Connects the component to other components or sets parameters and attributes.

        Args:
            **kwargs: Keyword arguments representing the connections, parameters, and attributes.

        Returns:
            None

        Raises:
            KeyError: If the specified input name does not exist.
        """
        for key, value in kwargs.items():
            self._process_connection_or_parameter(key, value)
        return self

    def list_inputs(self):
        """
        Returns a list of input names.
        """
        return [_input.name for _input in self.inputs]

    def list_outputs(self):
        """
        Returns a list of output names.
        """
        return [_output.name for _output in self.outputs]

    async def run(self):
        """
        Executes the component's logic and returns the result.

        Returns:
            The result of executing the component's logic.
        """
        return await self._run()

    def set_vertex(self, vertex: "Vertex"):
        """
        Sets the vertex for the component.

        Args:
            vertex (Vertex): The vertex to set.

        Returns:
            None
        """
        self._vertex = vertex

    def get_input(self, name: str) -> Any:
        """
        Retrieves the value of the input with the specified name.

        Args:
            name (str): The name of the input.

        Returns:
            Any: The value of the input.

        Raises:
            ValueError: If the input with the specified name is not found.
        """
        if name in self._inputs:
            return self._inputs[name]
        raise ValueError(f"Input {name} not found in {self.__class__.__name__}")

    def get_output(self, name: str) -> Any:
        """
        Retrieves the output with the specified name.

        Args:
            name (str): The name of the output to retrieve.

        Returns:
            Any: The output value.

        Raises:
            ValueError: If the output with the specified name is not found.
        """
        if name in self._outputs:
            return self._outputs[name]
        raise ValueError(f"Output {name} not found in {self.__class__.__name__}")

    def set_output_value(self, name: str, value: Any):
        if name in self._outputs:
            self._outputs[name].value = value
        else:
            raise ValueError(f"Output {name} not found in {self.__class__.__name__}")

    def map_outputs(self, outputs: List[Output]):
        """
        Maps the given list of outputs to the component.

        Args:
            outputs (List[Output]): The list of outputs to be mapped.

        Raises:
            ValueError: If the output name is None.

        Returns:
            None
        """
        self.outputs = outputs
        for output in outputs:
            if output.name is None:
                raise ValueError("Output name cannot be None.")
            self._outputs[output.name] = output

    def map_inputs(self, inputs: List["InputTypes"]):
        """
        Maps the given inputs to the component.

        Args:
            inputs (List[InputTypes]): A list of InputTypes objects representing the inputs.

        Raises:
            ValueError: If the input name is None.

        """
        self.inputs = inputs
        for input_ in inputs:
            if input_.name is None:
                raise ValueError("Input name cannot be None.")
            self._inputs[input_.name] = input_

    def validate(self, params: dict):
        """
        Validates the component parameters.

        Args:
            params (dict): A dictionary containing the component parameters.

        Raises:
            ValueError: If the inputs are not valid.
            ValueError: If the outputs are not valid.
        """
        self._validate_inputs(params)
        self._validate_outputs()

    def _set_output_types(self):
        for output in self.outputs:
            return_types = self._get_method_return_type(output.method)
            output.add_types(return_types)
            output.set_selected()

    def _get_output_by_method(self, method: Callable):
        # method is a callable and output.method is a string
        # we need to find the output that has the same method
        output = next((output for output in self.outputs if output.method == method.__name__), None)
        if output is None:
            method_name = method.__name__ if hasattr(method, "__name__") else str(method)
            raise ValueError(f"Output with method {method_name} not found")
        return output

    def _inherits_from_component(self, method: Callable):
        # check if the method is a method from a class that inherits from Component
        # and that it is an output of that class
        inherits_from_component = hasattr(method, "__self__") and isinstance(method.__self__, Component)
        return inherits_from_component

    def _method_is_valid_output(self, method: Callable):
        # check if the method is a method from a class that inherits from Component
        # and that it is an output of that class
        method_is_output = (
            hasattr(method, "__self__")
            and isinstance(method.__self__, Component)
            and method.__self__._get_output_by_method(method)
        )
        return method_is_output

    def _process_connection_or_parameter(self, key, value):
        _input = self._get_or_create_input(key)
        # We need to check if callable AND if it is a method from a class that inherits from Component
        if callable(value) and self._inherits_from_component(value):
            try:
                self._method_is_valid_output(value)
            except ValueError:
                raise ValueError(
                    f"Method {value.__name__} is not a valid output of {value.__self__.__class__.__name__}"
                )
            self._connect_to_component(key, value, _input)
        else:
            self._set_parameter_or_attribute(key, value)

    def _get_or_create_input(self, key):
        try:
            return self._inputs[key]
        except KeyError:
            _input = self._get_fallback_input(name=key, display_name=key)
            self._inputs[key] = _input
            self.inputs.append(_input)
            return _input

    def _connect_to_component(self, key, value, _input):
        component = value.__self__
        self._components.append(component)
        output = component._get_output_by_method(value)
        self._add_edge(component, key, output, _input)

    def _add_edge(self, component, key, output, _input):
        self._edges.append(
            {
                "source": component._id,
                "target": self._id,
                "data": {
                    "sourceHandle": {
                        "dataType": self.name,
                        "id": component._id,
                        "name": output.name,
                        "output_types": output.types,
                    },
                    "targetHandle": {
                        "fieldName": key,
                        "id": self._id,
                        "inputTypes": _input.input_types,
                        "type": _input.field_type,
                    },
                },
            }
        )

    def _set_parameter_or_attribute(self, key, value):
        self._set_input_value(key, value)
        self._parameters[key] = value
        self._attributes[key] = value

    def __call__(self, **kwargs):
        self.set(**kwargs)

        return run_until_complete(self.run())

    async def _run(self):
        # Resolve callable inputs
        for key, _input in self._inputs.items():
            if callable(_input.value):
                result = _input.value()
                if inspect.iscoroutine(result):
                    result = await result
                self._inputs[key].value = result

        self.set_attributes({})

        return await self.build_results()

    def __getattr__(self, name: str) -> Any:
        if "_attributes" in self.__dict__ and name in self.__dict__["_attributes"]:
            return self.__dict__["_attributes"][name]
        if "_inputs" in self.__dict__ and name in self.__dict__["_inputs"]:
            return self.__dict__["_inputs"][name].value
        if name in BACKWARDS_COMPATIBLE_ATTRIBUTES:
            return self.__dict__[f"_{name}"]
        raise AttributeError(f"{name} not found in {self.__class__.__name__}")

    def _set_input_value(self, name: str, value: Any):
        if name in self._inputs:
            input_value = self._inputs[name].value
            if callable(input_value):
                raise ValueError(
                    f"Input {name} is connected to {input_value.__self__.display_name}.{input_value.__name__}"
                )
            self._inputs[name].value = value
            if hasattr(self._inputs[name], "load_from_db"):
                self._inputs[name].load_from_db = False
        else:
            raise ValueError(f"Input {name} not found in {self.__class__.__name__}")

    def _validate_outputs(self):
        # Raise Error if some rule isn't met
        pass

    def _map_parameters_on_frontend_node(self, frontend_node: ComponentFrontendNode):
        for name, value in self._parameters.items():
            frontend_node.set_field_value_in_template(name, value)

    def _map_parameters_on_template(self, template: dict):
        for name, value in self._parameters.items():
            template[name]["value"] = value

    def _get_method_return_type(self, method_name: str) -> List[str]:
        method = getattr(self, method_name)
        return_type = get_type_hints(method)["return"]
        extracted_return_types = self._extract_return_type(return_type)
        return [format_type(extracted_return_type) for extracted_return_type in extracted_return_types]

    def _update_template(self, frontend_node: dict):
        return frontend_node

    def to_frontend_node(self):
        #! This part here is clunky but we need it like this for
        #! backwards compatibility. We can change how prompt component
        #! works and then update this later
        field_config = self.get_template_config(self)
        frontend_node = ComponentFrontendNode.from_inputs(**field_config)
        self._map_parameters_on_frontend_node(frontend_node)

        frontend_node_dict = frontend_node.to_dict(keep_name=False)
        frontend_node_dict = self._update_template(frontend_node_dict)
        self._map_parameters_on_template(frontend_node_dict["template"])

        frontend_node = ComponentFrontendNode.from_dict(frontend_node_dict)
        if not self._code:
            self.set_class_code()
        code_field = Input(
            dynamic=True,
            required=True,
            placeholder="",
            multiline=True,
            value=self._code,
            password=False,
            name="code",
            advanced=True,
            field_type="code",
            is_list=False,
        )
        frontend_node.template.add_field(code_field)

        for output in frontend_node.outputs:
            if output.types:
                continue
            return_types = self._get_method_return_type(output.method)
            output.add_types(return_types)
            output.set_selected()

        frontend_node.validate_component()
        frontend_node.set_base_classes_from_outputs()
        data = {
            "data": {
                "node": frontend_node.to_dict(keep_name=False),
                "type": self.__class__.__name__,
            }
        }
        return data

    def _validate_inputs(self, params: dict):
        # Params keys are the `name` attribute of the Input objects
        for key, value in params.copy().items():
            if key not in self._inputs:
                continue
            input_ = self._inputs[key]
            # BaseInputMixin has a `validate_assignment=True`

            input_.value = value
            params[input_.name] = input_.value

    def set_attributes(self, params: dict):
        self._validate_inputs(params)
        _attributes = {}
        for key, value in params.items():
            if key in self.__dict__ and value != getattr(self, key):
                raise ValueError(
                    f"{self.__class__.__name__} defines an input parameter named '{key}' "
                    f"that is a reserved word and cannot be used."
                )
            _attributes[key] = value
        for key, input_obj in self._inputs.items():
            if key not in _attributes:
                _attributes[key] = input_obj.value or None
        self._attributes = _attributes

    def _set_outputs(self, outputs: List[dict]):
        self.outputs = [Output(**output) for output in outputs]
        for output in self.outputs:
            setattr(self, output.name, output)

    def get_trace_as_inputs(self):
        predefined_inputs = {
            input_.name: input_.value
            for input_ in self.inputs
            if hasattr(input_, "trace_as_input") and input_.trace_as_input
        }
        # Dynamic inputs
        dynamic_inputs = {key: value for key, value in self._attributes.items() if key not in predefined_inputs}
        return {**predefined_inputs, **dynamic_inputs}

    def get_trace_as_metadata(self):
        return {
            input_.name: input_.value
            for input_ in self.inputs
            if hasattr(input_, "trace_as_metadata") and input_.trace_as_metadata
        }

    async def _build_with_tracing(self):
        inputs = self.get_trace_as_inputs()
        metadata = self.get_trace_as_metadata()
        async with self._tracing_service.trace_context(self, self.trace_name, inputs, metadata):
            _results, _artifacts = await self._build_results()
            self._tracing_service.set_outputs(self.trace_name, _results)

        return _results, _artifacts

    async def _build_without_tracing(self):
        return await self._build_results()

    async def build_results(self):
        if self._tracing_service:
            return await self._build_with_tracing()
        return await self._build_without_tracing()

    async def _build_results(self):
        _results = {}
        _artifacts = {}
        if hasattr(self, "outputs"):
            if self._vertex:
                self._set_outputs(self._vertex.outputs)
            for output in self.outputs:
                # Build the output if it's connected to some other vertex
                # or if it's not connected to any vertex
                if (
                    not self._vertex
                    or not self._vertex.outgoing_edges
                    or output.name in self._vertex.edges_source_names
                ):
                    if output.method is None:
                        raise ValueError(f"Output {output.name} does not have a method defined.")
                    method: Callable = getattr(self, output.method)
                    if output.cache and output.value != UNDEFINED:
                        _results[output.name] = output.value
                    else:
                        result = method()
                        # If the method is asynchronous, we need to await it
                        if inspect.iscoroutinefunction(method):
                            result = await result
                        if (
                            self._vertex is not None
                            and isinstance(result, Message)
                            and result.flow_id is None
                            and self._vertex.graph.flow_id is not None
                        ):
                            result.set_flow_id(self._vertex.graph.flow_id)
                        _results[output.name] = result
                        output.value = result
                        custom_repr = self.custom_repr()
                        if custom_repr is None and isinstance(result, (dict, Data, str)):
                            custom_repr = result
                        if not isinstance(custom_repr, str):
                            custom_repr = str(custom_repr)
                        raw = result
                        if self.status is None:
                            artifact_value = raw
                        else:
                            artifact_value = self.status
                            raw = self.status

                        if hasattr(raw, "data") and raw is not None:
                            raw = raw.data
                        if raw is None:
                            raw = custom_repr

                        elif hasattr(raw, "model_dump") and raw is not None:
                            raw = raw.model_dump()
                        if raw is None and isinstance(result, (dict, Data, str)):
                            raw = result.data if isinstance(result, Data) else result
                        artifact_type = get_artifact_type(artifact_value, result)
                        raw, artifact_type = post_process_raw(raw, artifact_type)
                        artifact = {"repr": custom_repr, "raw": raw, "type": artifact_type}
                        _artifacts[output.name] = artifact
                        self._output_logs[output.name] = self._logs
                        self._logs = []
        self._artifacts = _artifacts
        self._results = _results
        if self._tracing_service:
            self._tracing_service.set_outputs(self.trace_name, _results)
        return _results, _artifacts

    def custom_repr(self):
        if self.repr_value == "":
            self.repr_value = self.status
        if isinstance(self.repr_value, dict):
            return yaml.dump(self.repr_value)
        if isinstance(self.repr_value, str):
            return self.repr_value
        if isinstance(self.repr_value, BaseModel) and not isinstance(self.repr_value, Data):
            return str(self.repr_value)
        return self.repr_value

    def build_inputs(self, user_id: Optional[Union[str, UUID]] = None):
        """
        Builds the inputs for the custom component.

        Args:
            user_id (Optional[Union[str, UUID]], optional): The user ID. Defaults to None.

        Returns:
            List[Input]: The list of inputs.
        """
        # This function is similar to build_config, but it will process the inputs
        # and return them as a dict with keys being the Input.name and values being the Input.model_dump()
        self.inputs = self.template_config.get("inputs", [])
        if not self.inputs:
            return {}
        build_config = {_input.name: _input.model_dump(by_alias=True, exclude_none=True) for _input in self.inputs}
        return build_config

    def _get_field_order(self):
        try:
            inputs = self.template_config["inputs"]
            return [field.name for field in inputs]
        except KeyError:
            return []

    def build(self, **kwargs):
        self.set_attributes(kwargs)

    def _get_fallback_input(self, **kwargs):
        return Input(**kwargs)
