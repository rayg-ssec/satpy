#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2015-2018 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""Shared objects of the various reader classes."""

import logging
import os
import warnings
from datetime import datetime, timedelta

import yaml

try:
    from yaml import UnsafeLoader
except ImportError:
    from yaml import Loader as UnsafeLoader

from satpy.config import (config_search_paths, get_environ_config_dir,
                          glob_config)
from .yaml_reader import (AbstractYAMLReader,
                          load_yaml_configs as load_yaml_reader_configs)

LOG = logging.getLogger(__name__)


# Old Name -> New Name
OLD_READER_NAMES = {
}


def group_files(files_to_sort, reader=None, time_threshold=10,
                group_keys=None, ppp_config_dir=None, reader_kwargs=None):
    """Group series of files by file pattern information.

    By default this will group files by their filename ``start_time``
    assuming it exists in the pattern. By passing the individual
    dictionaries returned by this function to the Scene classes'
    ``filenames``, a series `Scene` objects can be easily created.

    .. versionadded:: 0.12

    Args:
        files_to_sort (iterable): File paths to sort in to group
        reader (str or Collection[str]): Reader or readers whose file patterns
            should be used to sort files.  If not given, try all readers (slow,
            adding a list of readers is strongly recommended).
        time_threshold (int): Number of seconds used to consider time elements
            in a group as being equal. For example, if the 'start_time' item
            is used to group files then any time within `time_threshold`
            seconds of the first file's 'start_time' will be seen as occurring
            at the same time.
        group_keys (list or tuple): File pattern information to use to group
            files. Keys are sorted in order and only the first key is used when
            comparing datetime elements with `time_threshold` (see above). This
            means it is recommended that datetime values should only come from
            the first key in ``group_keys``. Otherwise, there is a good chance
            that files will not be grouped properly (datetimes being barely
            unequal). Defaults to a reader's ``group_keys`` configuration (set
            in YAML), otherwise ``('start_time',)``.  When passing multiple
            readers, passing group_keys is strongly recommended as the
            behaviour without doing so is undefined.
        ppp_config_dir (str): Root usser configuration directory for Satpy.
            This will be deprecated in the future, but is here for consistency
            with other Satpy features.
        reader_kwargs (dict): Additional keyword arguments to pass to reader
            creation.

    Returns:
        List of dictionaries mapping 'reader' to a list of filenames.
        Each of these dictionaries can be passed as ``filenames`` to
        a `Scene` object.

    """
    if reader is not None and not isinstance(reader, (list, tuple)):
        reader = [reader]

    reader_kwargs = reader_kwargs or {}

    reader_files = _assign_files_to_readers(
            files_to_sort, reader, ppp_config_dir, reader_kwargs)

    if reader is None:
        reader = reader_files.keys()

    file_keys = _get_file_keys_for_reader_files(
            reader_files, group_keys=group_keys)

    file_groups = _get_sorted_file_groups(file_keys, time_threshold)

    return [{rn: file_groups[group_key].get(rn, []) for rn in reader} for group_key in file_groups]


def _assign_files_to_readers(files_to_sort, reader_names, ppp_config_dir,
                             reader_kwargs):
    """Assign files to readers.

    Given a list of file names (paths), match those to reader instances.

    Internal helper for group_files.

    Args:
        files_to_sort (Collection[str]): Files to assign to readers.
        reader_names (Collection[str]): Readers to consider
        ppp_config_dir (str):
        reader_kwargs (Mapping):

    Returns:
        Mapping[str, Tuple[reader, Set[str]]]
        Mapping where the keys are reader names and the values are tuples of
        (reader_configs, filenames).
    """
    files_to_sort = set(files_to_sort)
    reader_dict = {}
    for reader_configs in configs_for_reader(reader_names, ppp_config_dir):
        try:
            reader = load_reader(reader_configs, **reader_kwargs)
        except yaml.constructor.ConstructorError:
            LOG.exception(
                    f"ConstructorError loading {reader_configs!s}, "
                    "probably a missing dependency, skipping "
                    "corresponding reader (if you did not explicitly "
                    "specify the reader, Satpy tries all; performance "
                    "will improve if you pass readers explicitly).")
            continue
        reader_name = reader.info["name"]
        files_matching = set(reader.filter_selected_filenames(files_to_sort))
        files_to_sort -= files_matching
        if files_matching or reader_names is not None:
            reader_dict[reader_name] = (reader, files_matching)
    if files_to_sort:
        raise ValueError("No matching readers found for these files: " +
                         ", ".join(files_to_sort))
    return reader_dict


def _get_file_keys_for_reader_files(reader_files, group_keys=None):
    """From a mapping from _assign_files_to_readers, get file keys.

    Given a mapping where each key is a reader name and each value is a
    tuple of reader instance (typically FileYAMLReader) and a collection
    of files, return a mapping with the same keys, but where the values are
    lists of tuples of (keys, filename), where keys are extracted from the filenames
    according to group_keys and filenames are the names those keys were
    extracted from.

    Internal helper for group_files.

    Returns:
        Mapping[str, List[Tuple[Tuple, str]]], as described.
    """
    file_keys = {}
    for (reader_name, (reader_instance, files_to_sort)) in reader_files.items():
        if group_keys is None:
            group_keys = reader_instance.info.get('group_keys', ('start_time',))
        file_keys[reader_name] = []
        # make a copy because filename_items_for_filetype will modify inplace
        files_to_sort = set(files_to_sort)
        for _, filetype_info in reader_instance.sorted_filetype_items():
            for f, file_info in reader_instance.filename_items_for_filetype(files_to_sort, filetype_info):
                group_key = tuple(file_info.get(k) for k in group_keys)
                if all(g is None for g in group_key):
                    warnings.warn(
                            f"Found matching file {f:s} for reader "
                            "{reader_name:s}, but none of group keys found. "
                            "Group keys requested: " + ", ".join(group_keys),
                            UserWarning)
                file_keys[reader_name].append((group_key, f))
    return file_keys


def _get_sorted_file_groups(all_file_keys, time_threshold):
    """Get sorted file groups.

    Get a list of dictionaries, where each list item consists of a dictionary
    mapping a tuple of keys to a mapping of reader names to files.  The files
    listed in each list item are considered to be grouped within the same time.

    Args:
        all_file_keys, as returned by _get_file_keys_for_reader_files
        time_threshold: temporal threshold

    Returns:
        List[Mapping[Tuple, Mapping[str, List[str]]]], as described

    Internal helper for group_files.
    """
    # flatten to get an overall sorting; put the name in the middle in the
    # interest of sorting
    flat_keys = ((v[0], rn, v[1]) for (rn, vL) in all_file_keys.items() for v in vL)
    prev_key = None
    threshold = timedelta(seconds=time_threshold)
    # file_groups is sorted, because dictionaries are sorted by insertion
    # order in Python 3.7+
    file_groups = {}
    for gk, rn, f in sorted(flat_keys):
        # use first element of key as time identifier (if datetime type)
        if prev_key is None:
            is_new_group = True
            prev_key = gk
        elif isinstance(gk[0], datetime):
            # datetimes within threshold difference are "the same time"
            is_new_group = (gk[0] - prev_key[0]) > threshold
        else:
            is_new_group = gk[0] != prev_key[0]

        # compare keys for those that are found for both the key and
        # this is a generator and is not computed until the if statement below
        # when we know that `prev_key` is not None
        vals_not_equal = (this_val != prev_val for this_val, prev_val in zip(gk[1:], prev_key[1:])
                          if this_val is not None and prev_val is not None)
        # if this is a new group based on the first element
        if is_new_group or any(vals_not_equal):
            file_groups[gk] = {rn: [f]}
            prev_key = gk
        else:
            if rn not in file_groups[prev_key]:
                file_groups[prev_key][rn] = [f]
            else:
                file_groups[prev_key][rn].append(f)
    return file_groups


def read_reader_config(config_files, loader=UnsafeLoader):
    """Read the reader `config_files` and return the extracted reader metadata."""
    reader_config = load_yaml_reader_configs(*config_files, loader=loader)
    return reader_config['reader']


def load_reader(reader_configs, **reader_kwargs):
    """Import and setup the reader from *reader_info*."""
    return AbstractYAMLReader.from_config_files(*reader_configs, **reader_kwargs)


def configs_for_reader(reader=None, ppp_config_dir=None):
    """Generate reader configuration files for one or more readers.

    Args:
        reader (Optional[str]): Yield configs only for this reader
        ppp_config_dir (Optional[str]): Additional configuration directory
            to search for reader configuration files.

    Returns: Generator of lists of configuration files

    """
    search_paths = (ppp_config_dir,) if ppp_config_dir else tuple()
    if reader is not None:
        if not isinstance(reader, (list, tuple)):
            reader = [reader]
        # check for old reader names
        new_readers = []
        for reader_name in reader:
            if reader_name.endswith('.yaml') or reader_name not in OLD_READER_NAMES:
                new_readers.append(reader_name)
                continue

            new_name = OLD_READER_NAMES[reader_name]
            # Satpy 0.11 only displays a warning
            # Satpy 0.13 will raise an exception
            raise ValueError("Reader name '{}' has been deprecated, use '{}' instead.".format(reader_name, new_name))
            # Satpy 0.15 or 1.0, remove exception and mapping

        reader = new_readers
        # given a config filename or reader name
        config_files = [r if r.endswith('.yaml') else r + '.yaml' for r in reader]
    else:
        reader_configs = glob_config(os.path.join('readers', '*.yaml'),
                                     *search_paths)
        config_files = set(reader_configs)

    for config_file in config_files:
        config_basename = os.path.basename(config_file)
        reader_name = os.path.splitext(config_basename)[0]
        reader_configs = config_search_paths(
            os.path.join("readers", config_basename), *search_paths)

        if not reader_configs:
            # either the reader they asked for does not exist
            # or satpy is improperly configured and can't find its own readers
            raise ValueError("No reader named: {}".format(reader_name))

        yield reader_configs


def available_readers(as_dict=False):
    """Available readers based on current configuration.

    Args:
        as_dict (bool): Optionally return reader information as a dictionary.
                        Default: False

    Returns: List of available reader names. If `as_dict` is `True` then
             a list of dictionaries including additionally reader information
             is returned.

    """
    readers = []
    for reader_configs in configs_for_reader():
        try:
            reader_info = read_reader_config(reader_configs)
        except (KeyError, IOError, yaml.YAMLError):
            LOG.debug("Could not import reader config from: %s", reader_configs)
            LOG.debug("Error loading YAML", exc_info=True)
            continue
        readers.append(reader_info if as_dict else reader_info['name'])
    if as_dict:
        readers = sorted(readers, key=lambda reader_info: reader_info['name'])
    else:
        readers = sorted(readers)
    return readers


def find_files_and_readers(start_time=None, end_time=None, base_dir=None,
                           reader=None, sensor=None, ppp_config_dir=None,
                           filter_parameters=None, reader_kwargs=None,
                           missing_ok=False, fs=None):
    """Find files matching the provided parameters.

    Use `start_time` and/or `end_time` to limit found filenames by the times
    in the filenames (not the internal file metadata). Files are matched if
    they fall anywhere within the range specified by these parameters.

    Searching is **NOT** recursive.

    Files may be either on-disk or on a remote file system.  By default,
    files are searched for locally.  Users can search on remote filesystems by
    passing an instance of an implementation of
    `fsspec.spec.AbstractFileSystem` (strictly speaking, any object of a class
    implementing a ``glob`` method works).

    If locating files on a local file system, the returned dictionary
    can be passed directly to the `Scene` object through the `filenames`
    keyword argument.  If it points to a remote file system, it is the
    responsibility of the user to download the files first (directly
    reading from cloud storage is not currently available in Satpy).

    The behaviour of time-based filtering depends on whether or not the filename
    contains information about the end time of the data or not:

      - if the end time is not present in the filename, the start time of the filename
        is used and has to fall between (inclusive) the requested start and end times
      - otherwise, the timespan of the filename has to overlap the requested timespan

    Example usage for querying a s3 filesystem using the s3fs module:

    >>> import s3fs, satpy.readers, datetime
    >>> satpy.readers.find_files_and_readers(
    ...     base_dir="s3://noaa-goes16/ABI-L1b-RadF/2019/321/14/",
    ...     fs=s3fs.S3FileSystem(anon=True),
    ...     reader="abi_l1b",
    ...     start_time=datetime.datetime(2019, 11, 17, 14, 40))
    {'abi_l1b': [...]}

    Args:
        start_time (datetime): Limit used files by starting time.
        end_time (datetime): Limit used files by ending time.
        base_dir (str): The directory to search for files containing the
                        data to load. Defaults to the current directory.
        reader (str or list): The name of the reader to use for loading the data or a list of names.
        sensor (str or list): Limit used files by provided sensors.
        ppp_config_dir (str): The directory containing the configuration
                              files for Satpy.
        filter_parameters (dict): Filename pattern metadata to filter on. `start_time` and `end_time` are
                                  automatically added to this dictionary. Shortcut for
                                  `reader_kwargs['filter_parameters']`.
        reader_kwargs (dict): Keyword arguments to pass to specific reader
                              instances to further configure file searching.
        missing_ok (bool): If False (default), raise ValueError if no files
                            are found.  If True, return empty dictionary if no
                            files are found.
        fs (FileSystem): Optional, instance of implementation of
                         fsspec.spec.AbstractFileSystem (strictly speaking,
                         any object of a class implementing ``.glob`` is
                         enough).  Defaults to searching the local filesystem.

    Returns: Dictionary mapping reader name string to list of filenames

    """
    if ppp_config_dir is None:
        ppp_config_dir = get_environ_config_dir()
    reader_files = {}
    reader_kwargs = reader_kwargs or {}
    filter_parameters = filter_parameters or reader_kwargs.get('filter_parameters', {})
    sensor_supported = False

    if start_time or end_time:
        filter_parameters['start_time'] = start_time
        filter_parameters['end_time'] = end_time
    reader_kwargs['filter_parameters'] = filter_parameters

    for reader_configs in configs_for_reader(reader, ppp_config_dir):
        try:
            reader_instance = load_reader(reader_configs, **reader_kwargs)
        except (KeyError, IOError, yaml.YAMLError) as err:
            LOG.info('Cannot use %s', str(reader_configs))
            LOG.debug(str(err))
            if reader and (isinstance(reader, str) or len(reader) == 1):
                # if it is a single reader then give a more usable error
                raise
            continue

        if not reader_instance.supports_sensor(sensor):
            continue
        elif sensor is not None:
            # sensor was specified and a reader supports it
            sensor_supported = True
        loadables = reader_instance.select_files_from_directory(base_dir, fs)
        if loadables:
            loadables = list(
                reader_instance.filter_selected_filenames(loadables))
        if loadables:
            reader_files[reader_instance.name] = list(loadables)

    if sensor and not sensor_supported:
        raise ValueError("Sensor '{}' not supported by any readers".format(sensor))

    if not (reader_files or missing_ok):
        raise ValueError("No supported files found")
    return reader_files


def load_readers(filenames=None, reader=None, reader_kwargs=None,
                 ppp_config_dir=None):
    """Create specified readers and assign files to them.

    Args:
        filenames (iterable or dict): A sequence of files that will be used to load data from. A ``dict`` object
                                      should map reader names to a list of filenames for that reader.
        reader (str or list): The name of the reader to use for loading the data or a list of names.
        reader_kwargs (dict): Keyword arguments to pass to specific reader instances.
        ppp_config_dir (str): The directory containing the configuration files for satpy.

    Returns: Dictionary mapping reader name to reader instance

    """
    reader_instances = {}
    reader_kwargs = reader_kwargs or {}
    reader_kwargs_without_filter = reader_kwargs.copy()
    reader_kwargs_without_filter.pop('filter_parameters', None)

    if ppp_config_dir is None:
        ppp_config_dir = get_environ_config_dir()

    if not filenames and not reader:
        # used for an empty Scene
        return {}
    elif reader and filenames is not None and not filenames:
        # user made a mistake in their glob pattern
        raise ValueError("'filenames' was provided but is empty.")
    elif not filenames:
        LOG.warning("'filenames' required to create readers and load data")
        return {}
    elif reader is None and isinstance(filenames, dict):
        # filenames is a dictionary of reader_name -> filenames
        reader = list(filenames.keys())
        remaining_filenames = set(f for fl in filenames.values() for f in fl)
    elif reader and isinstance(filenames, dict):
        # filenames is a dictionary of reader_name -> filenames
        # but they only want one of the readers
        filenames = filenames[reader]
        remaining_filenames = set(filenames or [])
    else:
        remaining_filenames = set(filenames or [])

    for idx, reader_configs in enumerate(configs_for_reader(reader, ppp_config_dir)):
        if isinstance(filenames, dict):
            readers_files = set(filenames[reader[idx]])
        else:
            readers_files = remaining_filenames

        try:
            reader_instance = load_reader(reader_configs, **reader_kwargs)
        except (KeyError, IOError, yaml.YAMLError) as err:
            LOG.info('Cannot use %s', str(reader_configs))
            LOG.debug(str(err))
            continue

        if not readers_files:
            # we weren't given any files for this reader
            continue
        loadables = reader_instance.select_files_from_pathnames(readers_files)
        if loadables:
            reader_instance.create_filehandlers(loadables, fh_kwargs=reader_kwargs_without_filter)
            reader_instances[reader_instance.name] = reader_instance
            remaining_filenames -= set(loadables)
        if not remaining_filenames:
            break

    if remaining_filenames:
        LOG.warning("Don't know how to open the following files: {}".format(str(remaining_filenames)))
    if not reader_instances:
        raise ValueError("No supported files found")
    elif not any(list(r.available_dataset_ids) for r in reader_instances.values()):
        raise ValueError("No dataset could be loaded. Either missing "
                         "requirements (such as Epilog, Prolog) or none of the "
                         "provided files match the filter parameters.")
    return reader_instances
