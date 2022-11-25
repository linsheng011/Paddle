# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

from trt_layer_auto_scan_test import TrtLayerAutoScanTest
from program_config import TensorConfig, ProgramConfig
import unittest
import numpy as np
import paddle.inference as paddle_infer
from functools import partial
from typing import List


# This is the special test case with weight including batch dimension
# I don't want to mess up the code written by others, so I wrote a class specifically
class TrtConvertElementwiseTest_one_input_special_case0(TrtLayerAutoScanTest):
    def is_program_valid(self, program_config: ProgramConfig) -> bool:
        return True

    def sample_program_configs(self):
        def generate_input(shape):
            return np.random.random(shape).astype(np.float32)

        def generate_weight():
            return np.random.random([1, 32, 16, 32]).astype(np.float32)

        for batch in [1]:
            for shape in [[batch, 32, 16, 32]]:
                for op_type in ["logical_and", "logical_or", "logical_xor"]:
                    for axis in [-1]:
                        self.dims = len(shape)
                        dics = [
                            {"axis": axis},
                            {"in_dtype": 5, "out_dtype": 0},
                            {"in_dtype": 0, "out_dtype": 5},
                        ]
                        ops_config = [
                            {
                                "op_type": "cast",
                                "op_inputs": {"X": ["input_data"]},
                                "op_outputs": {"Out": ["cast_output_data1"]},
                                "op_attrs": dics[1],
                            },
                            {
                                "op_type": "cast",
                                "op_inputs": {"X": ["input_data"]},
                                "op_outputs": {"Out": ["cast_output_data3"]},
                                "op_attrs": dics[1],
                            },
                            {
                                "op_type": op_type,
                                "op_inputs": {
                                    "X": ["cast_output_data1"],
                                    "Y": ["cast_output_data3"],
                                },
                                "op_outputs": {"Out": ["cast_output_data0"]},
                                "op_attrs": dics[0],
                            },
                            {
                                "op_type": "cast",
                                "op_inputs": {"X": ["cast_output_data0"]},
                                "op_outputs": {"Out": ["output_data"]},
                                "op_attrs": dics[2],
                            },
                        ]
                        ops = self.generate_op_config(ops_config)

                        program_config = ProgramConfig(
                            ops=ops,
                            weights={
                                "weight": TensorConfig(
                                    data_gen=partial(generate_weight)
                                )
                            },
                            inputs={
                                "input_data": TensorConfig(
                                    data_gen=partial(generate_input, shape)
                                ),
                            },
                            outputs=["output_data"],
                        )

                        yield program_config

    def sample_predictor_configs(
        self, program_config
    ) -> (paddle_infer.Config, List[int], float):
        def generate_dynamic_shape(attrs):
            # The input.dims[1] must be equal to the weight's length.
            if self.dims == 4:
                self.dynamic_shape.min_input_shape = {
                    "input_data": [1, 32, 16, 32],
                    "cast_output_data1": [1, 32, 16, 32],
                    "cast_output_data3": [1, 32, 16, 32],
                    "cast_output_data0": [1, 32, 16, 32],
                }
                self.dynamic_shape.max_input_shape = {
                    "input_data": [1, 32, 16, 32],
                    "cast_output_data1": [1, 32, 16, 32],
                    "cast_output_data3": [1, 32, 16, 32],
                    "cast_output_data0": [1, 32, 16, 32],
                }
                self.dynamic_shape.opt_input_shape = {
                    "input_data": [1, 32, 16, 32],
                    "cast_output_data1": [1, 32, 16, 32],
                    "cast_output_data3": [1, 32, 16, 32],
                    "cast_output_data0": [1, 32, 16, 32],
                }

        def clear_dynamic_shape():
            self.dynamic_shape.max_input_shape = {}
            self.dynamic_shape.min_input_shape = {}
            self.dynamic_shape.opt_input_shape = {}

        def generate_trt_nodes_num(attrs, dynamic_shape):
            if dynamic_shape:
                ver = paddle_infer.get_trt_compile_version()
                if ver[0] * 1000 + ver[1] * 100 + ver[2] * 10 < 8400:
                    return 0, 6
                return 1, 2
            return 0, 6

        attrs = [
            program_config.ops[i].attrs for i in range(len(program_config.ops))
        ]

        # for static_shape
        clear_dynamic_shape()
        self.trt_param.precision = paddle_infer.PrecisionType.Float32
        yield self.create_inference_config(), generate_trt_nodes_num(
            attrs, False
        ), 1e-5
        self.trt_param.precision = paddle_infer.PrecisionType.Half
        yield self.create_inference_config(), generate_trt_nodes_num(
            attrs, False
        ), (1e-3, 1e-3)

        # for dynamic_shape
        generate_dynamic_shape(attrs)
        self.trt_param.precision = paddle_infer.PrecisionType.Float32
        yield self.create_inference_config(), generate_trt_nodes_num(
            attrs, True
        ), 1e-5
        self.trt_param.precision = paddle_infer.PrecisionType.Half
        yield self.create_inference_config(), generate_trt_nodes_num(
            attrs, True
        ), (1e-3, 1e-3)

    def add_skip_trt_case(self):
        pass

    def test(self):
        self.add_skip_trt_case()
        self.run_test()


if __name__ == "__main__":
    unittest.main()
