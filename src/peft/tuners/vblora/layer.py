# Copyright 2023-present the HuggingFace Inc. team.
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

import warnings
from typing import List, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.pytorch_utils import Conv1D

from peft.tuners.tuners_utils import BaseTunerLayer, check_adapters_to_merge
from peft.utils.other import transpose


class VBLoRALayer(BaseTunerLayer):
    # List all names of layers that may contain adapter weights
    adapter_layer_names = ("vblora_logits_A", "vblora_logits_B", "vblora_vector_bank")

    def __init__(self, base_layer: nn.Module, **kwargs):
        self.base_layer = base_layer
        self.r = {}
        self.topk = {}
        self.vblora_dropout = nn.ModuleDict({})

        # For storing vector scale
        self.vblora_logits_A = nn.ParameterDict({}) 
        self.vblora_logits_B = nn.ParameterDict({}) 
        self.vblora_vector_bank = nn.ParameterDict({})

        # Mark the weight as unmerged
        self._disable_adapters = False
        self.merged_adapters = []

        base_layer = self.get_base_layer()
        if isinstance(base_layer, nn.Linear):
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif isinstance(base_layer, Conv1D):
            in_features, out_features = (
                base_layer.weight.ds_shape if hasattr(base_layer.weight, "ds_shape") else base_layer.weight.shape
            )

        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs

    @property
    def merged(self) -> bool:
        return bool(self.merged_adapters)

    def update_layer(
        self,
        adapter_name,
        vblora_vector_bank,
        r,
        topk,
        num_vectors,
        vector_length,
        vblora_dropout,
        init_weights,
    ):
        if r <= 0:
            raise ValueError(f"`r` {r} should be a positive integer value")
        if topk <= 0:
            raise ValueError(f"`topk` {topk} should be a positive integer value")
        
        if self.in_features % vector_length != 0:
            raise ValueError(f'`in_features` {self.in_features} must be divisible by `vector_length` {vector_length}')
        if self.out_features % vector_length != 0:
            raise ValueError(f'`out_features` {self.out_features} must be divisible by `vector_length` {vector_length}')
        
        self.r[adapter_name] = r
        self.topk[adapter_name] = topk
        if vblora_dropout > 0.0:
            vblora_dropout_layer = nn.Dropout(p=vblora_dropout)
        else:
            vblora_dropout_layer = nn.Identity()

        self.vblora_dropout.update(nn.ModuleDict({adapter_name: vblora_dropout_layer}))
        self.vblora_logits_A[adapter_name] = nn.Parameter(torch.zeros(self.in_features//vector_length, r, num_vectors), requires_grad=True)
        self.vblora_logits_B[adapter_name] = nn.Parameter(torch.zeros(self.out_features//vector_length, r, num_vectors), requires_grad=True)
        self.vblora_vector_bank = vblora_vector_bank

        if adapter_name not in vblora_vector_bank:
            # This means that this is not the first VeRA adapter. We have to add an entry in the dict for this adapter.
            if len(self.vblora_vector_bank) < 1:
                raise ValueError(
                    "The `vector bank` is empty. This should not happen. Please report this issue."
                )
            # we can take any of the existing adapter's parameters, as they should all be identical
            vblora_vector_bank_param = list(self.vblora_vector_bank.values())[0]
            self.vblora_vector_bank[adapter_name] = vblora_vector_bank_param

        if init_weights:
            self.reset_vblora_parameters(adapter_name)

        weight = getattr(self.get_base_layer(), "weight", None)
        if weight is not None:
            # the layer is already completely initialized, this is an update
            if weight.dtype.is_floating_point or weight.dtype.is_complex:
                self.to(weight.device, dtype=weight.dtype)
            else:
                self.to(weight.device)

        self.set_adapter(self.active_adapters)

    def reset_vblora_parameters(self, adapter_name):
        if adapter_name in self.vblora_logits_A.keys():
            with torch.no_grad():
                nn.init.normal_(self.vblora_logits_A[adapter_name], 0, 0.01)
                nn.init.normal_(self.vblora_logits_B[adapter_name], 0, 0.01)


class Linear(nn.Linear, VBLoRALayer):
    # Vera implemented in a dense layer
    def __init__(
        self,
        base_layer,
        vblora_vector_bank,
        adapter_name: str,
        r: int,
        num_vectors: int, 
        vector_length: int,
        topk: int = 2, 
        vblora_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        is_target_conv_1d_layer: bool = False,
        init_weights: bool = True,
        **kwargs,
    ) -> None:
        # this gets the init from nn.Linear's super perspective, i.e. nn.Module.__init__, which should always be called
        super(nn.Linear, self).__init__()
        VBLoRALayer.__init__(self, base_layer, **kwargs)
        self.fan_in_fan_out = fan_in_fan_out
        self._active_adapter = adapter_name
        self.update_layer(adapter_name, vblora_vector_bank, r, topk, num_vectors, vector_length, vblora_dropout, init_weights,)
        self.is_target_conv_1d_layer = is_target_conv_1d_layer

    def merge(self, safe_merge: bool = False, adapter_names: Optional[List[str]] = None) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`List[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        adapter_names = check_adapters_to_merge(self, adapter_names)
        if not adapter_names:
            # no adapter to merge
            return

        for active_adapter in adapter_names:
            if active_adapter in self.vblora_logits_A.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()

                    orig_weights += self.get_delta_weight(active_adapter)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    base_layer.weight.data += self.get_delta_weight(active_adapter)
                self.merged_adapters.append(active_adapter)

    def unmerge(self) -> None:
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return

        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.vblora_logits_A.keys():
                self.get_base_layer().weight.data -= self.get_delta_weight(active_adapter)

    @staticmethod
    def _get_low_rank_matrix(logits, vblora_vector_bank, topk):
        top_k_logits, indices = logits.topk(topk, dim=-1)
        topk_weights = F.softmax(top_k_logits, dim=-1)
        return (topk_weights.unsqueeze(-1) * vblora_vector_bank[indices]).sum(-2)

    def get_delta_weight(self, adapter) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        vblora_logits_A = self.vblora_logits_A[adapter]
        vblora_logits_B = self.vblora_logits_B[adapter]
        vblora_vector_bank = self.vblora_vector_bank[adapter]

        device = vblora_logits_A.device
        dtype = vblora_logits_A.dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16
        if cast_to_fp32:
            vblora_logits_A = vblora_logits_A.float()
            vblora_logits_B = vblora_logits_B.float()
            vblora_vector_bank = vblora_vector_bank.float()

        A = Linear._get_low_rank_matrix(vblora_logits_A, vblora_vector_bank, self.topk).transpose(1, 2).reshape(-1, vblora_logits_A.shape[1])
        B = Linear._get_low_rank_matrix(vblora_logits_B, vblora_vector_bank, self.topk).transpose(0, 1).reshape(vblora_logits_B.shape[1], -1)
        output_tensor = transpose(A @ B, self.fan_in_fan_out)
        return output_tensor

    def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        previous_dtype = x.dtype

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            for active_adapter in self.active_adapters:
                if active_adapter not in self.vblora_logits_A.keys():
                    continue
                
                vblora_logits_A = self.vblora_logits_A[active_adapter]
                vblora_logits_B = self.vblora_logits_B[active_adapter]
                vblora_vector_bank = self.vblora_vector_bank[active_adapter]
                topk = self.topk[active_adapter]
                dropout = self.vblora_dropout[active_adapter]
                x = x.to(vblora_logits_A.dtype)
                # (tile, rank, vector_length) -> (tile, vector_length, rank) -> (tile x vector_length, rank)
                A = Linear._get_low_rank_matrix(vblora_logits_A, vblora_vector_bank, topk).transpose(1, 2).reshape(-1, vblora_logits_A.shape[1]) 
                # (tile, rank, vector_length) -> (rank, tile, vector_length) -> (rank, tile x vector_length)
                B = Linear._get_low_rank_matrix(vblora_logits_B, vblora_vector_bank, topk).transpose(0, 1).reshape(vblora_logits_B.shape[1], -1)

                result = result + dropout(x) @ A @ B

        result = result.to(previous_dtype)
        return result
