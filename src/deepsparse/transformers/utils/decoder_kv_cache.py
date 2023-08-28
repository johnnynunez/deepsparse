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

from typing import Any, Dict, List

import numpy

from deepsparse.engine import LIB


__all__ = ["DecoderKVCache", "SEQUENCE_LENGTH_AXIS"]


SEQUENCE_LENGTH_AXIS = 2


class DecoderKVCache:
    def __init__(self, use_deepsparse_cache: bool = False):
        """
        The goal this object is to handle the manipulation
        of the key value cache.

        :param use_deepsparse_cache: If set to True, the `kv_cache` object
            from the deepsparse.LIB will be loaded as an attribute.
            This object is used to handle the manipulation of the
            key/value buffers on the DeepSparse engine side.
        """
        self.total_num_processed_tokens = None

        # assuming that kv cache arrays are of shape
        # [batch_size, num_heads, sequence_length, hidden_size]
        self._sequence_len_axis = SEQUENCE_LENGTH_AXIS
        self._use_deepsparse_cache = use_deepsparse_cache
        self._session_id = None
        self._freeze_first_position = None
        self._state = None
        self._kv_cache = None

    def setup(
        self,
        session_id: str,
        state: Dict[str, Any],
        num_processed_tokens: int = 0,
        freeze_first_position: bool = False,
    ):
        """
        Setup the session - a level of abstraction that allocates
        the resources to store and manipulate the kv cache.

        :param session_id: The session id to use for the current
            session. Used to identify the kv cache state
        :param state: The state of the cache. This is a dictionary
            that maps the name of the cache array to the cache array.
            The cache tensor is a numpy array of shape
            [batch_size, num_heads, sequence_length - num_input_ids, hidden_size]
        :param num_processed_tokens: The number of tokens processed so far.
        :param freeze_first_position: If set to True, once the kv cache
            gets filled, the position along the sequence length axis
            that corresponds to the first token will be frozen.
            This assures that, once the KV cache is full (there are no
            "blank" entries), and we are removing the "oldest" entry
            from the cache, we will nevertheless keep the cache entry
            that corresponds to the BOS token in the sequence.
            By default, is set to False.
        """
        self._session_id = session_id
        self._state = state
        self._freeze_first_position = freeze_first_position
        self.total_num_processed_tokens = num_processed_tokens

        if self._use_deepsparse_cache:
            prev_num_tokens = self.total_num_processed_tokens
            num_frozen_tokens = int(self._freeze_first_position)
            self._kv_cache = LIB.kv_cache(prev_num_tokens, num_frozen_tokens)

    def update(
        self,
        state: Dict[str, Any],
        input_ids_len: int,
    ):
        """
        Updating the session is identical with taking the kv cache
        output from the forward pass and restructuring it, so it
        can be directly used as input for the next forward pass.

        :param state: The state of the cache. This is a dictionary
            that maps the name of the cache array to the cache array.
            The cache tensor is a numpy array of shape
            [batch_size, num_heads, sequence_length, hidden_size]
        :param input_ids_len: The number of input ids in the current
            input batch: (batch_size, length).
            Corresponds to `input_ids.shape[1]`
        """
        self.total_num_processed_tokens += input_ids_len

        input_state_capacity = state[list(state.keys())[0]].shape[
            self._sequence_len_axis
        ]
        num_entries_to_delete = input_ids_len

        # compute the number of blank (padded) entries in the cache
        num_padded_entries = max(
            0, input_state_capacity - self.total_num_processed_tokens
        )
        # compute how many of those entries need to be deleted
        num_padded_entries_to_delete = min(num_padded_entries, num_entries_to_delete)

        # if we had fewer padded entries than num_entries_to_delete,
        # we additionally are forced to delete some non-padded entries (the oldest ones)
        num_non_padded_entries_to_delete = max(
            0, num_entries_to_delete - num_padded_entries
        )

        for name, cache_array in state.items():
            if num_padded_entries_to_delete:
                cache_array = self.remove_padded_entries(
                    cache_array, num_padded_entries_to_delete
                )
            if num_non_padded_entries_to_delete:
                cache_array = self.remove_non_padded_entries(
                    cache_array, num_entries_to_delete
                )
            state[name] = numpy.ascontiguousarray(cache_array)

        self._state = state

    def remove_padded_entries(
        self, cache_array: numpy.ndarray, num_padded_entries_to_delete: int
    ):
        """
        Remove the num_padded_entries_to_delete entries from the cache array.
        This function assumes that the cache_array has the number
        of padded (blank) entries that is equal/larger than
        num_padded_entries_to_delete.

        :param cache_array: The cache array to be modified.
        :param num_padded_entries_to_delete: The number of padded entries to delete.
        """
        return cache_array[:, :, num_padded_entries_to_delete:, :]

    def remove_non_padded_entries(
        self, cache_array: numpy.ndarray, num_non_padded_entries_to_delete: int
    ):
        """
        Remove the num_non_padded_entries_to_delete entries from the cache array.
        This function assumes that the cache_array has no padded (blank) entries and
        thus we are forced to delete the oldest entries from the cache.

        If self._freeze_first_position is set to True, that means that the oldest
        entry in the cache_array is the one that corresponds to the BOS token. Because
        we want to keep that entry in the cache, we will delete the oldest entry
        starting from the second oldest entry.
        """
        new_cache_array = cache_array[
            :,
            :,
            bool(self._freeze_first_position) + num_non_padded_entries_to_delete :,
            :,
        ]
        if self._freeze_first_position:
            bos_entries = cache_array[:, :, :1, :]
            new_cache_array = numpy.concatenate(
                [bos_entries, new_cache_array], axis=self._sequence_len_axis
            )
        return new_cache_array

    def set_capacity(self, capacity: int):
        """
        Enforce a new total capacity for the state
        of cached inputs.

        This means popping the old entries if the new
        total capacity should lesser than the current one

        or

        Padding the state blank entries if the new
        total capacity should be greater than the current one

        :param capacity: The new length of the
            self._state in the
            `self._sequence_length_axis` dimension
        """
        capacity_difference = self.capacity - capacity
        state = self.cached_inputs

        if capacity_difference > 0:
            raise NotImplementedError(
                "The scenario when capacity"
                "needs to be expanded is not yet"
                "supported."
            )

        elif capacity_difference < 0:
            indices = [0] * abs(capacity_difference)
            state = self._add_entries(state, indices=indices)

        else:
            return

        self._state = state

    def _add_entries(
        self, state: Dict[str, Any], indices: List[int], padding_value: int = 0
    ) -> Dict[str, Any]:
        for key, value in state.items():
            # required to make sure that both
            # quantized and non quantized caches
            # are supported
            state_dtype = value.dtype
            # change padding_value dtype to match the state dtype
            padding_value = numpy.array(padding_value, dtype=state_dtype)

            state[key] = numpy.insert(
                value, indices, padding_value, axis=self._sequence_len_axis
            )
        return state

    @property
    def id(self):
        if self._session_id is None:
            raise ValueError("Attempted to access session_id before setting up session")
        return self._session_id

    @property
    def num_non_blank_entries(self):
        """
        :return: the number of non-blank entries in the kv cache
        """
        return min(self.capacity, self.total_num_processed_tokens)

    @property
    def capacity(self) -> int:
        """
        Return the maximum number of kv cache entries
        that the decoder can hold, until the old entries
        start to get erased to make place for new entries

        :return: the maximum number of kv cache entries
            that the decoder can hold
        """
        return self.cached_inputs[list(self.cached_inputs.keys())[0]].shape[
            self._sequence_len_axis
        ]

    @id.setter
    def id(self, session_id: str):
        self._session_id = session_id

    @property
    def cached_inputs(self):
        return self._state