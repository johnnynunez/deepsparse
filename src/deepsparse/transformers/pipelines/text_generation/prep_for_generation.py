# Copyright (c) 2021 - present / Neuralmagic, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
from typing import Any, Optional

import numpy
from pydantic import BaseModel, Field

from deepsparse.operators import Operator
from deepsparse.transformers.pipelines.text_generation import TokenGeneratorOperator
from deepsparse.transformers.schemas.text_generation_schemas import FinishReason
from deepsparse.transformers.utils.helpers import set_generated_length
from deepsparse.utils import InferenceState


__all__ = ["PrepareGeneration", "PrepareForGenerationOutput"]


class PrepareForGenerationOutput(BaseModel):
    prompt_logits: Any = Field(
        description="A set of prompt logits generated during prefill"
    )
    kv_cache: Optional[Any] = Field(description="kv cache")
    in_generation: Optional[bool] = Field(description="in_generation flag")


class PrepareGeneration(Operator):
    output_schema = PrepareForGenerationOutput

    def __init__(
        self,
        token_generator: TokenGeneratorOperator,
        prompt_sequence_length: int,
        sequence_length: int,
    ):
        self.sequence_length = sequence_length
        self.token_generator_creator = token_generator
        self.prompt_sequence_length = prompt_sequence_length

    def can_operate(self, inp: Any):
        kv_cache = inp.get("kv_cache")
        tokens = inp.get("tokens")

        # If the number of prompt tokens is greater
        # than what we've processed, don't start generation.
        # Should be equal when started as all prompt logits
        # should be accounted for, and we should have updated
        # the kv_cache for the single token engine.
        if len(tokens) == kv_cache.total_num_processed_tokens:
            return True
        return False

    def run(
        self, tokens: Any, kv_cache: Any, inference_state: InferenceState, **kwargs
    ):
        prompt_logits = inference_state.current_state.get("prompt_logits")
        prompt_logits = numpy.concatenate(prompt_logits, axis=1)
        # TODO: clean this up such that dont have to keep writing current_state
        # everywhere

        generation_config = inference_state.current_state.get("generation_config")
        include_prompt_logits = inference_state.current_state.get(
            "include_prompt_logits"
        )

        token_generator_creator_output = self.token_generator_creator.run(
            logits_shape=prompt_logits[0, -1, :].shape,
            deterministic=not generation_config.do_sample,
            sampling_temperature=generation_config.temperature,
            tokens=copy.copy(tokens),
            **inference_state.current_state,
        )
        token_generator = token_generator_creator_output.get("token_generator")

        max_tokens, length_finish_reason = set_generated_length(
            max_length=generation_config.max_length,
            prompt_tokens_length=1,
            max_new_tokens=generation_config.max_new_tokens,
            sequence_length=self.sequence_length,
            prompt_sequence_length=self.prompt_sequence_length,
            finish_reason_choices=FinishReason,
        )

        state_update = {
            "max_tokens": max_tokens,
            "length_finish_reason": length_finish_reason,
            "generated_tokens": [],
            "generated_logits": [prompt_logits[:, 0:-1, :]]
            if include_prompt_logits
            else [],
            "finished_reason": [],
            "token_generator": token_generator,
        }

        if kv_cache is None:
            output = {"prompt_logits": numpy.expand_dims(prompt_logits[:, -1, :], 0)}
        else:
            output = {
                "kv_cache": kv_cache,
                "in_generation": True,
                "prompt_logits": numpy.expand_dims(prompt_logits[:, -1, :], 0),
            }

        return output, state_update
