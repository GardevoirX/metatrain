import copy
from math import prod
from pathlib import Path
from typing import Dict, List, Optional, Union

import metatensor.torch
import torch
from metatensor.torch import Labels, TensorBlock, TensorMap
from metatensor.torch.atomistic import (
    MetatensorAtomisticModel,
    ModelCapabilities,
    ModelMetadata,
    ModelOutput,
    NeighborListOptions,
    System,
)

from ...utils.additive import ZBL, CompositionModel
from ...utils.data import DatasetInfo, TargetInfo
from ...utils.dtype import dtype_to_str
from .modules.encoder import Encoder
from .modules.nef import (
    edge_array_to_nef,
    get_corresponding_edges,
    get_nef_indices,
    nef_array_to_edges,
)
from .modules.radial_mask import get_radial_mask
from .modules.structures import concatenate_structures
from .modules.transformer import Transformer


class NanoPET(torch.nn.Module):
    """
    Re-implementation of the PET architecture (https://arxiv.org/pdf/2305.19302).

    The positions and atomic species are encoded into a high-dimensional space
    using a simple encoder. The resulting features (in NEF, or Node-Edge-Feature
    format*) are then processed by a series of transformer layers. This process is
    repeated for a number of message-passing layers, where features are exchanged
    between corresponding edges (ij and ji). The final representation is used to
    predict atomic properties through decoders named "heads".

    * NEF format: a three-dimensional tensor where the first dimension corresponds
    to the nodes, the second to the edges corresponding to the neighbors of the
    node (padded as different nodes might have different numbers of edges),
    and the third to the features.
    """

    __supported_devices__ = ["cuda", "cpu"]
    __supported_dtypes__ = [torch.float64, torch.float32]

    component_labels: Dict[str, List[Labels]]

    def __init__(self, model_hypers: Dict, dataset_info: DatasetInfo) -> None:
        super().__init__()

        for target in dataset_info.targets.values():
            if target.is_spherical:
                raise ValueError(
                    "The NanoPET model does not support spherical tensor targets. "
                    "Only scalar and Cartesian tensor targets are supported."
                )

        self.hypers = model_hypers
        self.dataset_info = dataset_info
        self.new_outputs = list(dataset_info.targets.keys())
        self.atomic_types = dataset_info.atomic_types

        self.requested_nl = NeighborListOptions(
            cutoff=self.hypers["cutoff"],
            full_list=True,
            strict=True,
        )

        self.cutoff = self.hypers["cutoff"]
        self.cutoff_width = self.hypers["cutoff_width"]

        self.encoder = Encoder(len(self.atomic_types), self.hypers["d_pet"])

        self.transformer = Transformer(
            self.hypers["d_pet"],
            4 * self.hypers["d_pet"],
            self.hypers["num_heads"],
            self.hypers["num_attention_layers"],
            0.0,  # MLP dropout rate
            0.0,  # attention dropout rate
        )
        # empirically, the model seems to perform better without dropout

        self.num_mp_layers = self.hypers["num_gnn_layers"] - 1
        gnn_contractions = []
        gnn_transformers = []
        for _ in range(self.num_mp_layers):
            gnn_contractions.append(
                torch.nn.Linear(
                    2 * self.hypers["d_pet"], self.hypers["d_pet"], bias=False
                )
            )
            gnn_transformers.append(
                Transformer(
                    self.hypers["d_pet"],
                    4 * self.hypers["d_pet"],
                    self.hypers["num_heads"],
                    self.hypers["num_attention_layers"],
                    0.0,  # MLP dropout rate
                    0.0,  # attention dropout rate
                )
            )
        self.gnn_contractions = torch.nn.ModuleList(gnn_contractions)
        self.gnn_transformers = torch.nn.ModuleList(gnn_transformers)

        self.last_layer_feature_size = self.hypers["d_pet"]

        # register the outputs
        # the model is always capable of outputting the last layer features
        self.outputs = {
            "mtt::aux::last_layer_features": ModelOutput(unit="unitless", per_atom=True)
        }
        self.last_layers = torch.nn.ModuleDict()
        self.output_shapes: Dict[str, List[int]] = {}
        for target_name, target_info in dataset_info.targets.items():
            self._add_output(target_name, target_info)

        self.register_buffer(
            "species_to_species_index",
            torch.full(
                (max(self.atomic_types) + 1,),
                -1,
            ),
        )
        for i, species in enumerate(self.atomic_types):
            self.species_to_species_index[species] = i

        # additive models: these are handled by the trainer at training
        # time, and they are added to the output at evaluation time
        composition_model = CompositionModel(
            model_hypers={},
            dataset_info=dataset_info,
        )
        additive_models = [composition_model]
        if self.hypers["zbl"]:
            additive_models.append(ZBL(model_hypers, dataset_info))
        self.additive_models = torch.nn.ModuleList(additive_models)

        # cache keys, components, properties labels
        self.single_label = Labels.single()
        self.key_labels = {
            output_name: copy.deepcopy(dataset_info.targets[output_name].layout.keys)
            for output_name in self.outputs.keys()
            if "mtt::aux::" not in output_name
        }
        self.component_labels = {
            output_name: copy.deepcopy(
                dataset_info.targets[output_name].layout.block().components
            )
            for output_name in self.outputs.keys()
            if "mtt::aux::" not in output_name
        }
        self.property_labels = {
            output_name: copy.deepcopy(
                dataset_info.targets[output_name].layout.block().properties
            )
            for output_name in self.outputs.keys()
            if "mtt::aux::" not in output_name
        }

    def restart(self, dataset_info: DatasetInfo) -> "NanoPET":
        # merge old and new dataset info
        merged_info = self.dataset_info.union(dataset_info)
        new_atomic_types = [
            at for at in merged_info.atomic_types if at not in self.atomic_types
        ]
        new_targets = {
            key: value
            for key, value in merged_info.targets.items()
            if key not in self.dataset_info.targets
        }

        if len(new_atomic_types) > 0:
            raise ValueError(
                f"New atomic types found in the dataset: {new_atomic_types}. "
                "The nanoPET model does not support adding new atomic types."
            )

        # register new outputs as new last layers
        for target_name, target in new_targets.items():
            self._add_output(target_name, target)

        self.dataset_info = merged_info
        self.atomic_types = sorted(self.atomic_types)

        return self

    def forward(
        self,
        systems: List[System],
        outputs: Dict[str, ModelOutput],
        selected_atoms: Optional[Labels] = None,
    ) -> Dict[str, TensorMap]:
        # Checks on systems (species) and outputs are done in the
        # MetatensorAtomisticModel wrapper

        device = systems[0].device

        if self.single_label.values.device != device:
            self.single_label = self.single_label.to(device)
            self.key_labels = {
                output_name: label.to(device)
                for output_name, label in self.key_labels.items()
            }
            self.component_labels = {
                output_name: [label.to(device) for label in labels]
                for output_name, labels in self.component_labels.items()
            }
            self.property_labels = {
                output_name: label.to(device)
                for output_name, label in self.property_labels.items()
            }

        system_indices = torch.concatenate(
            [
                torch.full(
                    (len(system),),
                    i_system,
                    device=device,
                )
                for i_system, system in enumerate(systems)
            ],
        )

        sample_values = torch.stack(
            [
                system_indices,
                torch.concatenate(
                    [
                        torch.arange(
                            len(system),
                            device=device,
                        )
                        for system in systems
                    ],
                ),
            ],
            dim=1,
        )
        sample_labels = Labels(
            names=["system", "atom"],
            values=sample_values,
        )

        (
            positions,
            centers,
            neighbors,
            species,
            cells,
            cell_shifts,
        ) = concatenate_structures(systems, self.requested_nl)

        # somehow the backward of this operation is very slow at evaluation,
        # where there is only one cell, therefore we simplify the calculation
        # for that case
        if len(cells) == 1:
            cell_contributions = cell_shifts.to(cells.dtype) @ cells[0]
        else:
            cell_contributions = torch.einsum(
                "ab, abc -> ac",
                cell_shifts.to(cells.dtype),
                cells[system_indices[centers]],
            )

        edge_vectors = positions[neighbors] - positions[centers] + cell_contributions

        bincount = torch.bincount(centers)
        if bincount.numel() == 0:  # no edges
            max_edges_per_node = 0
        else:
            max_edges_per_node = int(torch.max(bincount))

        # Convert to NEF (Node-Edge-Feature) format:
        nef_indices, nef_to_edges_neighbor, nef_mask = get_nef_indices(
            centers, len(positions), max_edges_per_node
        )

        # Get radial mask
        r = torch.sqrt(torch.sum(edge_vectors**2, dim=-1))
        radial_mask = get_radial_mask(r, self.cutoff, self.cutoff - self.cutoff_width)

        # Element indices
        element_indices_nodes = self.species_to_species_index[species]
        element_indices_centers = element_indices_nodes[centers]
        element_indices_neighbors = element_indices_nodes[neighbors]

        # Send everything to NEF:
        edge_vectors = edge_array_to_nef(edge_vectors, nef_indices)
        radial_mask = edge_array_to_nef(
            radial_mask, nef_indices, nef_mask, fill_value=0.0
        )
        element_indices_centers = edge_array_to_nef(
            element_indices_centers, nef_indices
        )
        element_indices_neighbors = edge_array_to_nef(
            element_indices_neighbors, nef_indices
        )

        features = {
            "cartesian": edge_vectors,
            "center": element_indices_centers,
            "neighbor": element_indices_neighbors,
        }

        # Encode
        features = self.encoder(features)

        # Transformer
        features = self.transformer(features, radial_mask)

        # GNN
        if self.num_mp_layers > 0:
            corresponding_edges = get_corresponding_edges(
                torch.concatenate(
                    [centers.unsqueeze(-1), neighbors.unsqueeze(-1), cell_shifts],
                    dim=-1,
                )
            )
            for contraction, transformer in zip(
                self.gnn_contractions, self.gnn_transformers
            ):
                new_features = nef_array_to_edges(
                    features, centers, nef_to_edges_neighbor
                )
                corresponding_new_features = new_features[corresponding_edges]
                new_features = torch.concatenate(
                    [new_features, corresponding_new_features], dim=-1
                )
                new_features = contraction(new_features)
                new_features = edge_array_to_nef(new_features, nef_indices)
                new_features = transformer(new_features, radial_mask)
                features = (features + new_features) * 0.5**0.5

        edge_features = features * radial_mask[:, :, None]
        node_features = torch.sum(edge_features, dim=1)

        return_dict: Dict[str, TensorMap] = {}

        # output the hidden features, if requested:
        if "mtt::aux::last_layer_features" in outputs:
            last_layer_feature_tmap = TensorMap(
                keys=self.single_label,
                blocks=[
                    TensorBlock(
                        values=node_features,
                        samples=sample_labels,
                        components=[],
                        properties=Labels(
                            names=["properties"],
                            values=torch.arange(
                                node_features.shape[-1], device=node_features.device
                            ).reshape(-1, 1),
                        ),
                    )
                ],
            )
            last_layer_features_options = outputs["mtt::aux::last_layer_features"]
            if last_layer_features_options.per_atom:
                return_dict["mtt::aux::last_layer_features"] = last_layer_feature_tmap
            else:
                return_dict["mtt::aux::last_layer_features"] = (
                    metatensor.torch.sum_over_samples(last_layer_feature_tmap, ["atom"])
                )

        atomic_properties_tmap_dict: Dict[str, TensorMap] = {}
        for output_name, last_layer in self.last_layers.items():
            if output_name in outputs:
                atomic_properties = last_layer(node_features)
                block = TensorBlock(
                    values=atomic_properties.reshape(
                        [-1] + self.output_shapes[output_name]
                    ),
                    samples=sample_labels,
                    components=self.component_labels[output_name],
                    properties=self.property_labels[output_name],
                )
                atomic_properties_tmap_dict[output_name] = TensorMap(
                    keys=self.key_labels[output_name],
                    blocks=[block],
                )

        if selected_atoms is not None:
            for output_name, tmap in atomic_properties_tmap_dict.items():
                atomic_properties_tmap_dict[output_name] = metatensor.torch.slice(
                    tmap, axis="samples", selection=selected_atoms
                )

        for output_name, atomic_property in atomic_properties_tmap_dict.items():
            if outputs[output_name].per_atom:
                return_dict[output_name] = atomic_property
            else:
                return_dict[output_name] = metatensor.torch.sum_over_samples(
                    atomic_property, ["atom"]
                )

        if not self.training:
            # at evaluation, we also add the additive contributions
            for additive_model in self.additive_models:
                # some of the outputs might not be present in the additive model
                # (e.g. the composition model only provides outputs for scalar targets)
                outputs_for_additive_model: Dict[str, ModelOutput] = {}
                for output_name in outputs:
                    if output_name in additive_model.outputs:
                        outputs_for_additive_model[output_name] = outputs[output_name]
                additive_contributions = additive_model(
                    systems, outputs_for_additive_model, selected_atoms
                )
                for name in additive_contributions:
                    if name.startswith("mtt::aux::"):
                        continue  # skip auxiliary outputs (not targets)
                    return_dict[name] = metatensor.torch.add(
                        return_dict[name],
                        additive_contributions[name],
                    )

        return return_dict

    def requested_neighbor_lists(
        self,
    ) -> List[NeighborListOptions]:
        return [self.requested_nl]

    @classmethod
    def load_checkpoint(cls, path: Union[str, Path]) -> "NanoPET":

        # Load the checkpoint
        checkpoint = torch.load(path, weights_only=False, map_location="cpu")
        model_hypers = checkpoint["model_hypers"]
        model_state_dict = checkpoint["model_state_dict"]

        # Create the model
        model = cls(**model_hypers)
        state_dict_iter = iter(model_state_dict.values())
        next(state_dict_iter)  # skip `species_to_species_index` buffer (int)
        dtype = next(state_dict_iter).dtype
        model.to(dtype).load_state_dict(model_state_dict)

        return model

    def export(self) -> MetatensorAtomisticModel:
        dtype = next(self.parameters()).dtype
        if dtype not in self.__supported_dtypes__:
            raise ValueError(f"unsupported dtype {self.dtype} for NanoPET")

        # Make sure the model is all in the same dtype
        # For example, after training, the additive models could still be in
        # float64
        self.to(dtype)

        interaction_ranges = [self.hypers["num_gnn_layers"] * self.hypers["cutoff"]]
        for additive_model in self.additive_models:
            if hasattr(additive_model, "cutoff_radius"):
                interaction_ranges.append(additive_model.cutoff_radius)
        interaction_range = max(interaction_ranges)

        capabilities = ModelCapabilities(
            outputs=self.outputs,
            atomic_types=self.atomic_types,
            interaction_range=interaction_range,
            length_unit=self.dataset_info.length_unit,
            supported_devices=self.__supported_devices__,
            dtype=dtype_to_str(dtype),
        )

        return MetatensorAtomisticModel(self.eval(), ModelMetadata(), capabilities)

    def _add_output(self, target_name: str, target_info: TargetInfo) -> None:

        self.output_shapes[target_name] = [
            len(comp.values) for comp in target_info.layout.block().components
        ] + [len(target_info.layout.block().properties.values)]
        self.outputs[target_name] = ModelOutput(
            quantity=target_info.quantity,
            unit=target_info.unit,
            per_atom=True,
        )
        self.last_layers[target_name] = torch.nn.Linear(
            self.hypers["d_pet"],
            prod(self.output_shapes[target_name]),
            bias=False,
        )
