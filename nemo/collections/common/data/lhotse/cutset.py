# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import logging
import warnings
from functools import partial
from itertools import repeat
from pathlib import Path
from typing import Sequence, Tuple

from lhotse import CutSet
from omegaconf import DictConfig, ListConfig

from nemo.collections.common.data.lhotse.nemo_adapters import LazyNeMoIterator, LazyNeMoTarredIterator


def read_cutset_from_config(config: DictConfig) -> Tuple[CutSet, bool]:
    """
    Reads NeMo configuration and creates a CutSet either from Lhotse or NeMo manifests.

    Returns a tuple of ``CutSet`` and a boolean indicating whether the data is tarred (True) or not (False).
    """
    # First, check if the dataset is specified in the new configuration format and use it if possible.
    if config.get("input_config") is not None:
        return read_dataset_config(config)
    # Now, we'll figure out if we should read Lhotse manifest or NeMo manifest.
    use_nemo_manifest = all(config.get(opt) is None for opt in ("cuts_path", "shar_path"))
    if use_nemo_manifest:
        assert (
            config.get("manifest_filepath") is not None
        ), "You must specify either: manifest_filepath, cuts_path, or shar_path"
        is_tarred = config.get("tarred_audio_filepaths") is not None
    else:
        is_tarred = config.get("shar_path") is not None
    if use_nemo_manifest:
        # Read NeMo manifest -- use the right wrapper depending on tarred/non-tarred.
        cuts = read_nemo_manifest(config, is_tarred)
    else:
        # Read Lhotse manifest (again handle both tarred(shar)/non-tarred).
        cuts = read_lhotse_manifest(config, is_tarred)
    return cuts, is_tarred


KNOWN_DATASET_CONFIG_TYPES = frozenset(("nemo", "nemo_tarred", "lhotse", "lhotse_shar", "group"))


def read_dataset_config(config) -> tuple[CutSet, bool]:
    """
    Input configuration format examples.
    Example 1. Combine two datasets with equal weights and attach custom metadata in ``tags`` to each cut::
        input_config:
          - name: dataset_1
            type: nemo_tarred
            manifest_filepath: /path/to/manifest__OP_0..512_CL_.json
            tarred_audio_filepath: /path/to/tarred_audio/audio__OP_0..512_CL_.tar
            weight: 0.5
            tags:
              lang: en
              some_metadata: some_value
          - name: dataset_2
            type: nemo_tarred
            manifest_filepath: /path/to/manifest__OP_0..512_CL_.json
            tarred_audio_filepath: /path/to/tarred_audio/audio__OP_0..512_CL_.tar
            weight: 0.5
            tags:
              lang: pl
              some_metadata: some_value
    Example 2. Combine multiple (4) datasets, with 2 corresponding to different tasks (ASR, AST).
        There are two levels of weights: per task (outer) and per dataset (inner).
        The final weight is the product of outer and inner weight::
        input_config:
          - name: asr_data
            type: group
            weight: 0.7
            tags:
              task: asr
            components:
              - name: dataset_1
                type: nemo_tarred
                manifest_filepath: /path/to/asr1/manifest__OP_0..512_CL_.json
                tarred_audio_filepath: /path/to/tarred_audio/asr1/audio__OP_0..512_CL_.tar
                weight: 0.6
                tags:
                  lang: en
                  some_metadata: some_value
              - name: dataset_2
                type: nemo_tarred
                manifest_filepath: /path/to/asr2/manifest__OP_0..512_CL_.json
                tarred_audio_filepath: /path/to/asr2/tarred_audio/audio__OP_0..512_CL_.tar
                weight: 0.4
                tags:
                  lang: pl
                  some_metadata: some_value
          - name: ast_data
            type: group
            weight: 0.3
            tags:
              task: ast
            components:
              - type: nemo_tarred
                manifest_filepath: /path/to/ast1/manifest__OP_0..512_CL_.json
                tarred_audio_filepath: /path/to/ast1/tarred_audio/audio__OP_0..512_CL_.tar
                weight: 0.2
                tags:
                  src_lang: en
                  tgt_lang: pl
              - type: nemo_tarred
                manifest_filepath: /path/to/ast2/manifest__OP_0..512_CL_.json
                tarred_audio_filepath: /path/to/ast2/tarred_audio/audio__OP_0..512_CL_.tar
                weight: 0.8
                tags:
                  src_lang: pl
                  tgt_lang: en
    """
    propagate_attrs = {
        "shuffle": config.shuffle,
        "shard_seed": config.shard_seed,
        "text_field": config.text_field,
        "lang_field": config.lang_field,
        "missing_sampling_rate_ok": config.missing_sampling_rate_ok,
        "max_open_streams": config.max_open_streams,
    }
    cuts, is_tarred = parse_and_combine_datasets(config.input_config, propagate_attrs=propagate_attrs)
    return cuts, is_tarred


def parse_group(grp_cfg: DictConfig, propagate_attrs: dict) -> [CutSet, bool]:
    assert grp_cfg.type in KNOWN_DATASET_CONFIG_TYPES, f"Unknown item type in dataset config list: {grp_cfg.type=}"
    if grp_cfg.type == "nemo_tarred":
        is_tarred = True
        cuts = read_nemo_manifest(grp_cfg, is_tarred=is_tarred)
    elif grp_cfg.type == "nemo":
        is_tarred = False
        cuts = read_nemo_manifest(grp_cfg, is_tarred=is_tarred)
    elif grp_cfg.type == "lhotse_shar":
        is_tarred = True
        cuts = read_lhotse_manifest(grp_cfg, is_tarred=is_tarred)
    elif grp_cfg.type == "lhotse":
        is_tarred = False
        cuts = read_lhotse_manifest(grp_cfg, is_tarred=is_tarred)
    elif grp_cfg.type == "group":
        cuts, is_tarred = parse_and_combine_datasets(grp_cfg.components, propagate_attrs=propagate_attrs,)
    else:
        raise ValueError(f"Unrecognized group: {grp_cfg.type}")
    # Attach extra tags to every utterance dynamically, if provided.
    if (extra_tags := grp_cfg.get("tags")) is not None:
        cuts = cuts.map(partial(attach_tags, tags=extra_tags))
    return cuts, is_tarred


def attach_tags(cut, tags: dict):
    for key, val in tags.items():
        setattr(cut, key, val)
    return cut


def parse_and_combine_datasets(
    config_list: list[DictConfig] | ListConfig, propagate_attrs: dict
) -> tuple[CutSet, bool]:
    cuts = []
    weights = []
    tarred_status = []
    assert len(config_list) > 0, "Empty group in dataset config list."

    for item in config_list:

        # Check if we have any attributes that are propagated downwards to each item in the group.
        # If a key already exists in the item, it takes precedence (we will not overwrite);
        # otherwise we will assign it.
        # We also update propagate_atts for the next sub-groups based on what's present in this group
        next_propagate_attrs = propagate_attrs.copy()
        for k, v in propagate_attrs.items():
            if k not in item:
                item[k] = v
            else:
                next_propagate_attrs[k] = item[k]

        # Load the item (which may also be another group) as a CutSet.
        item_cuts, item_is_tarred = parse_group(item, next_propagate_attrs)
        cuts.append(item_cuts)
        tarred_status.append(item_is_tarred)
        if (w := item.get("weight")) is not None:
            weights.append(w)

    assert all(t == tarred_status[0] for t in tarred_status), "Mixing tarred and non-tarred datasets is not supported."
    assert len(cuts) == len(
        weights
    ), "Missing dataset weight. When weighting datasets, every dataset must have a specified weight."
    cuts = mux(*cuts, weights=weights if weights else None, max_open_streams=propagate_attrs.get("max_open_streams"))
    return cuts, tarred_status[0]


def read_lhotse_manifest(config, is_tarred: bool) -> CutSet:
    if is_tarred:
        # Lhotse Shar is the equivalent of NeMo's native "tarred" dataset.
        # The combination of shuffle_shards, and repeat causes this to
        # be an infinite manifest that is internally reshuffled on each epoch.
        # The parameter ``config.shard_seed`` is used to determine shard shuffling order. Options:
        # - "trng" means we'll defer setting the seed until the iteration
        #   is triggered, and we'll use system TRNG to get a completely random seed for each worker.
        #   This results in every dataloading worker using full data but in a completely different order.
        # - "randomized" means we'll defer setting the seed until the iteration
        #   is triggered, and we'll use config.seed to get a pseudo-random seed for each worker.
        #   This results in every dataloading worker using full data but in a completely different order.
        #   Unlike "trng", this is deterministic, and if you resume training, you should change the seed
        #   to observe different data examples than in the previous run.
        # - integer means we'll set a specific seed in every worker, and data would be duplicated across them.
        #   This is mostly useful for unit testing or debugging.
        shard_seed = config.shard_seed
        if config.get("cuts_path") is not None:
            warnings.warn("Note: lhotse.cuts_path will be ignored because lhotse.shar_path was provided.")
        if isinstance(config.shar_path, (str, Path)):
            logging.info(f"Initializing Lhotse Shar CutSet (tarred) from a single data source: '{config.shar_path}'")
            cuts = CutSet.from_shar(in_dir=config.shar_path, shuffle_shards=True, seed=shard_seed).repeat()
        else:
            # Multiple datasets in Lhotse Shar format: we will dynamically multiplex them
            # with probability approximately proportional to their size
            logging.info(
                "Initializing Lhotse Shar CutSet (tarred) from multiple data sources with a weighted multiplexer. "
                "We found the following sources and weights: "
            )
            cutsets = []
            weights = []
            for item in config.shar_path:
                if isinstance(item, (str, Path)):
                    path = item
                    cs = CutSet.from_shar(in_dir=path, shuffle_shards=True, seed=shard_seed)
                    weight = len(cs)
                else:
                    assert isinstance(item, Sequence) and len(item) == 2 and isinstance(item[1], (int, float)), (
                        "Supported inputs types for config.shar_path are: "
                        "str | list[str] | list[tuple[str, number]] "
                        "where str is a path and number is a mixing weight (it may exceed 1.0). "
                        f"We got: '{item}'"
                    )
                    path, weight = item
                    cs = CutSet.from_shar(in_dir=path, shuffle_shards=True, seed=shard_seed)
                logging.info(f"- {path=} {weight=}")
                cutsets.append(cs.repeat())
                weights.append(weight)
            cuts = mux(*cutsets, weights=weights, max_open_streams=config.max_open_streams, seed=config.shard_seed)
    else:
        # Regular Lhotse manifest points to individual audio files (like native NeMo manifest).
        cuts = CutSet.from_file(config.cuts_path)
    return cuts


def read_nemo_manifest(config, is_tarred: bool) -> CutSet:
    common_kwargs = {
        "text_field": config.text_field,
        "lang_field": config.lang_field,
    }
    # The option below is to allow a special case of NeMo manifest iteration as Lhotse CutSet
    # without performing any I/O. NeMo manifests typically don't have sampling_rate information required by Lhotse.
    # This is useful for utility scripts that iterate metadata and estimate optimal batching settings.
    notar_kwargs = {"missing_sampling_rate_ok": config.missing_sampling_rate_ok}
    if isinstance(config.manifest_filepath, (str, Path)):
        logging.info(f"Initializing Lhotse CutSet from a single NeMo manifest (tarred): '{config.manifest_filepath}'")
        if is_tarred:
            cuts = CutSet(
                LazyNeMoTarredIterator(
                    config.manifest_filepath,
                    tar_paths=config.tarred_audio_filepaths,
                    shuffle_shards=config.shuffle,
                    **common_kwargs,
                )
            )
        else:
            cuts = CutSet(LazyNeMoIterator(config.manifest_filepath, **notar_kwargs, **common_kwargs))
    else:
        # Format option 1:
        #   Assume it's [[path1], [path2], ...] (same for tarred_audio_filepaths).
        #   This is the format for multiple NeMo buckets.
        #   Note: we set "weights" here to be proportional to the number of utterances in each data source.
        #         this ensures that we distribute the data from each source uniformly throughout each epoch.
        #         Setting equal weights would exhaust the shorter data sources closer the towards the beginning
        #         of an epoch (or over-sample it in the case of infinite CutSet iteration with .repeat()).
        # Format option 1:
        #   Assume it's [[path1, weight1], [path2, weight2], ...] (while tarred_audio_filepaths remain unchanged).
        #   Note: this option allows to manually set the weights for multiple datasets.
        logging.info(
            f"Initializing Lhotse CutSet from multiple tarred NeMo manifest sources with a weighted multiplexer. "
            f"We found the following sources and weights: "
        )
        cutsets = []
        weights = []
        tar_paths = config.tarred_audio_filepaths if is_tarred else repeat((None,))
        # Create a stream for each dataset.
        for manifest_info, (tar_path,) in zip(config.manifest_filepath, tar_paths):
            # First, convert manifest_path[+tar_path] to an iterator.
            manifest_path = manifest_info[0]
            if is_tarred:
                nemo_iter = LazyNeMoTarredIterator(
                    manifest_path=manifest_path, tar_paths=tar_path, shuffle_shards=config.shuffle, **common_kwargs
                )
            else:
                nemo_iter = LazyNeMoIterator(manifest_path, **notar_kwargs, **common_kwargs)
            # Then, determine the weight or use one provided
            if len(manifest_info) == 1:
                weight = len(nemo_iter)
            else:
                assert (
                    isinstance(manifest_info, Sequence)
                    and len(manifest_info) == 2
                    and isinstance(manifest_info[1], (int, float))
                ), (
                    "Supported inputs types for config.manifest_filepath are: "
                    "str | list[list[str]] | list[tuple[str, number]] "
                    "where str is a path and number is a mixing weight (it may exceed 1.0). "
                    f"We got: '{manifest_info}'"
                )
                weight = manifest_info[1]
            logging.info(f"- {manifest_path=} {weight=}")
            # [optional] When we have a limit on the number of open streams,
            #   split the manifest to individual shards if applicable.
            #   This helps the multiplexing achieve closer data distribution
            #   to the one desired in spite of the limit.
            if config.max_open_streams is not None:
                for subiter in nemo_iter.to_shards():
                    cutsets.append(CutSet(subiter))
                    weights.append(weight)
            else:
                cutsets.append(CutSet(nemo_iter))
                weights.append(weight)
        # Finally, we multiplex the dataset streams to mix the data.
        cuts = mux(*cutsets, weights=weights, max_open_streams=config.max_open_streams, seed=config.shard_seed)
    return cuts


def mux(
    *cutsets: CutSet, weights: list[int | float], max_open_streams: int | None = None, seed: str | int = "trng"
) -> CutSet:
    """
    Helper function to call the right multiplexing method flavour in lhotse.
    The result is always an infinitely iterable ``CutSet``, but depending on whether ``max_open_streams`` is set,
    it will select a more appropriate multiplexing strategy.
    """
    if max_open_streams is not None:
        cuts = CutSet.infinite_mux(*cutsets, weights=weights, seed=seed, max_open_streams=max_open_streams)
    else:
        cuts = CutSet.mux(*[cs.repeat() for cs in cutsets], weights=weights, seed=seed)
    return cuts
