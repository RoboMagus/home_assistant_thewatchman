"""Miscellaneous support functions for watchman"""

import anyio
import re
import fnmatch
import time
import logging
from datetime import datetime
from textwrap import wrap
import os
from typing import Any
import pytz
from prettytable import PrettyTable
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_HEADER,
    DEFAULT_CHUNK_SIZE,
    CONF_HEADER,
    CONF_IGNORED_ITEMS,
    CONF_IGNORED_STATES,
    CONF_CHUNK_SIZE,
    CONF_COLUMNS_WIDTH,
    CONF_FRIENDLY_NAMES,
    BUNDLED_IGNORED_ITEMS,
    DEFAULT_REPORT_FILENAME,
    HASS_DATA_CHECK_DURATION,
    HASS_DATA_FILES_IGNORED,
    HASS_DATA_FILES_PARSED,
    HASS_DATA_MISSING_ENTITIES,
    HASS_DATA_MISSING_SERVICES,
    HASS_DATA_PARSE_DURATION,
    HASS_DATA_PARSED_ENTITY_LIST,
    HASS_DATA_PARSED_SERVICE_LIST,
    REPORT_ENTRY_TYPE_ENTITY,
    REPORT_ENTRY_TYPE_SERVICE,
)

_LOGGER = logging.getLogger(__name__)


def get_config(hass: HomeAssistant, key, default):
    """get configuration value"""
    if DOMAIN_DATA not in hass.data:
        return default
    return hass.data[DOMAIN_DATA].get(key, default)


async def async_get_report_path(hass, path):
    """if path not specified, create report in config directory with default filename"""
    if not path:
        path = hass.config.path(DEFAULT_REPORT_FILENAME)
    folder, _ = os.path.split(path)
    if not await anyio.Path(folder).exists():
        raise HomeAssistantError(f"Incorrect report_path: {path}.")
    return path


def get_columns_width(user_width):
    """define width of the report columns"""
    default_width = [30, 7, 60]
    if not user_width:
        return default_width
    try:
        return [7 if user_width[i] < 7 else user_width[i] for i in range(3)]
    except (TypeError, IndexError):
        _LOGGER.error(
            "Invalid configuration for table column widths, default values" " used %s",
            default_width,
        )
    return default_width


def table_renderer(hass, entry_type):
    """Render ASCII tables in the report"""
    table = PrettyTable()
    columns_width = get_config(hass, CONF_COLUMNS_WIDTH, None)
    columns_width = get_columns_width(columns_width)
    if entry_type == REPORT_ENTRY_TYPE_SERVICE:
        services_missing = hass.data[DOMAIN][HASS_DATA_MISSING_SERVICES]
        service_list = hass.data[DOMAIN][HASS_DATA_PARSED_SERVICE_LIST]
        table.field_names = ["Service ID", "State", "Location"]
        for service in services_missing:
            row = [
                fill(service, columns_width[0]),
                fill("missing", columns_width[1]),
                fill(service_list[service], columns_width[2]),
            ]
            table.add_row(row)
        table.align = "l"
        return table.get_string()
    elif entry_type == REPORT_ENTRY_TYPE_ENTITY:
        entities_missing = hass.data[DOMAIN][HASS_DATA_MISSING_ENTITIES]
        parsed_entity_list = hass.data[DOMAIN][HASS_DATA_PARSED_ENTITY_LIST]
        friendly_names = get_config(hass, CONF_FRIENDLY_NAMES, False)
        header = ["Entity ID", "State", "Location"]
        table.field_names = header
        for entity in entities_missing:
            state, name = get_entity_state(hass, entity, friendly_names)
            table.add_row(
                [
                    fill(entity, columns_width[0], name),
                    fill(state, columns_width[1]),
                    fill(parsed_entity_list[entity], columns_width[2]),
                ]
            )

        table.align = "l"
        return table.get_string()

    else:
        return f"Table render error: unknown entry type: {entry_type}"


def text_renderer(hass, entry_type):
    """Render plain lists in the report"""
    result = ""
    if entry_type == REPORT_ENTRY_TYPE_SERVICE:
        services_missing = hass.data[DOMAIN][HASS_DATA_MISSING_SERVICES]
        service_list = hass.data[DOMAIN][HASS_DATA_PARSED_SERVICE_LIST]
        for service in services_missing:
            result += f"{service} in {fill(service_list[service], 0)}\n"
        return result
    elif entry_type == REPORT_ENTRY_TYPE_ENTITY:
        entities_missing = hass.data[DOMAIN][HASS_DATA_MISSING_ENTITIES]
        entity_list = hass.data[DOMAIN][HASS_DATA_PARSED_ENTITY_LIST]
        friendly_names = get_config(hass, CONF_FRIENDLY_NAMES, False)
        for entity in entities_missing:
            state, name = get_entity_state(hass, entity, friendly_names)
            entity_col = entity if not name else f"{entity} ('{name}')"
            result += f"{entity_col} [{state}] in: {fill(entity_list[entity], 0)}\n"

        return result
    else:
        return f"Text render error: unknown entry type: {entry_type}"


async def async_get_next_file(folder_tuples, ignored_files):
    """Returns next file for scan"""
    if not ignored_files:
        ignored_files = ""
    else:
        ignored_files = "|".join([f"({fnmatch.translate(f)})" for f in ignored_files])
    ignored_files_re = re.compile(ignored_files)
    for folder_name, glob_pattern in folder_tuples:
        _LOGGER.debug(
            "Scan folder %s with pattern %s for configuration files",
            folder_name,
            glob_pattern,
        )
        async for filename in anyio.Path(folder_name).glob(glob_pattern):
            _LOGGER.debug("Found file %s.", filename)
            yield (
                str(filename),
                (ignored_files and ignored_files_re.match(str(filename))),
            )


def add_entry(_list, entry, yaml_file, lineno):
    """Add entry to list of missing entities/services with line number information"""
    _LOGGER.debug("Added %s to the list", entry)
    if entry in _list:
        if yaml_file in _list[entry]:
            _list[entry].get(yaml_file, []).append(lineno)
    else:
        _list[entry] = {yaml_file: [lineno]}


def is_service(hass, entry):
    """check whether config entry is a service"""
    domain, service = entry.split(".")[0], ".".join(entry.split(".")[1:])
    return hass.services.has_service(domain, service)


def get_entity_state(hass, entry, friendly_names=False):
    """returns entity state or missing if entity does not extst"""
    entity = hass.states.get(entry)
    name = None
    if entity and entity.attributes.get("friendly_name", None):
        if friendly_names:
            name = entity.name
    # fix for #75, some integrations return non-string states
    state = (
        "missing" if not entity else str(entity.state).replace("unavailable", "unavail")
    )
    return state, name


def check_services(hass):
    """check if entries from config file are services"""
    services_missing = {}
    if "missing" in get_config(hass, CONF_IGNORED_STATES, []):
        return services_missing
    if (
        DOMAIN not in hass.data
        or HASS_DATA_PARSED_SERVICE_LIST not in hass.data[DOMAIN]
    ):
        raise HomeAssistantError("Service list not found")
    parsed_service_list = hass.data[DOMAIN][HASS_DATA_PARSED_SERVICE_LIST]
    _LOGGER.debug("::check_services")
    for entry, occurrences in parsed_service_list.items():
        if not is_service(hass, entry):
            services_missing[entry] = occurrences
            _LOGGER.debug("service %s added to missing list", entry)
    return services_missing


def check_entitites(hass):
    """check if entries from config file are entities with an active state"""
    ignored_states = [
        "unavail" if s == "unavailable" else s
        for s in get_config(hass, CONF_IGNORED_STATES, [])
    ]
    if DOMAIN not in hass.data or HASS_DATA_PARSED_ENTITY_LIST not in hass.data[DOMAIN]:
        _LOGGER.error("Entity list not found")
        raise Exception("Entity list not found")
    parsed_entity_list = hass.data[DOMAIN][HASS_DATA_PARSED_ENTITY_LIST]
    entities_missing = {}
    _LOGGER.debug("::check_entities")
    for entry, occurrences in parsed_entity_list.items():
        if is_service(hass, entry):  # this is a service, not entity
            _LOGGER.debug("entry %s is service, skipping", entry)
            continue
        state, _ = get_entity_state(hass, entry)
        if state in ignored_states:
            _LOGGER.debug("entry %s ignored due to ignored_states", entry)
            continue
        if state in ["missing", "unknown", "unavail"]:
            entities_missing[entry] = occurrences
            _LOGGER.debug("entry %s added to missing list", entry)
    return entities_missing


async def parse(hass, folders, ignored_files, root=None):
    """Parse a yaml or json file for entities/services"""
    files_parsed = 0
    entity_pattern = re.compile(
        r"(?:(?<=\s)|(?<=^)|(?<=\")|(?<=\'))([A-Za-z_0-9]*\s*:)?(?:\s*)?(?:states.)?"
        fr"(({ "|".join(Platform) })\.[A-Za-z_*0-9]+)"
    )
    service_pattern = re.compile(r"service:\s*([A-Za-z_0-9]*\.[A-Za-z_0-9]+)")
    comment_pattern = re.compile(r"\s*#.*")
    parsed_entity_list = {}
    parsed_service_list = {}
    effectively_ignored = []
    _LOGGER.debug("::parse started")
    async for yaml_file, ignored in async_get_next_file(folders, ignored_files):
        short_path = os.path.relpath(yaml_file, root)
        if ignored:
            effectively_ignored.append(short_path)
            _LOGGER.debug("%s ignored", yaml_file)
            continue

        try:
            lineno = 1
            async with await anyio.open_file(
                yaml_file, mode="r", encoding="utf-8"
            ) as f:
                async for line in f:
                    line = re.sub(comment_pattern, "", line)
                    for match in re.finditer(entity_pattern, line):
                        typ, val = match.group(1), match.group(2)
                        if (
                            typ != "service:"
                            and "*" not in val
                            and not val.endswith(".yaml")
                        ):
                            add_entry(parsed_entity_list, val, short_path, lineno)
                    for match in re.finditer(service_pattern, line):
                        val = match.group(1)
                        add_entry(parsed_service_list, val, short_path, lineno)
                    lineno += 1
            files_parsed += 1
            _LOGGER.debug("%s parsed", yaml_file)
        except OSError as exception:
            _LOGGER.error("Unable to parse %s: %s", yaml_file, exception)
        except UnicodeDecodeError as exception:
            _LOGGER.error(
                "Unable to parse %s: %s. Use UTF-8 encoding to avoid this error",
                yaml_file,
                exception,
            )

    # remove ignored entities and services from resulting lists
    ignored_items = get_config(hass, CONF_IGNORED_ITEMS, [])
    ignored_items = list(set(ignored_items + BUNDLED_IGNORED_ITEMS))
    excluded_entities = []
    excluded_services = []
    for itm in ignored_items:
        if itm:
            excluded_entities.extend(fnmatch.filter(parsed_entity_list, itm))
            excluded_services.extend(fnmatch.filter(parsed_service_list, itm))

    parsed_entity_list = {
        k: v for k, v in parsed_entity_list.items() if k not in excluded_entities
    }
    parsed_service_list = {
        k: v for k, v in parsed_service_list.items() if k not in excluded_services
    }

    _LOGGER.debug("Parsed files: %s", files_parsed)
    _LOGGER.debug("Ignored files: %s", effectively_ignored)
    _LOGGER.debug("Found entities: %s", len(parsed_entity_list))
    _LOGGER.debug("Found services: %s", len(parsed_service_list))
    return (
        parsed_entity_list,
        parsed_service_list,
        files_parsed,
        len(effectively_ignored),
    )


def fill(data, width, extra=None):
    """arrange data by table column width"""
    if data and isinstance(data, dict):
        key, val = next(iter(data.items()))
        out = f"{key}:{','.join([str(v) for v in val])}"
    else:
        out = str(data) if not extra else f"{data} ('{extra}')"

    return (
        "\n".join([out.ljust(width) for out in wrap(out, width)]) if width > 0 else out
    )


async def report(hass, render, chunk_size, test_mode=False):
    """generates watchman report either as a table or as a list"""
    if DOMAIN not in hass.data:
        raise HomeAssistantError("No data for report, refresh required.")

    start_time = time.time()
    header = get_config(hass, CONF_HEADER, DEFAULT_HEADER)
    services_missing = hass.data[DOMAIN][HASS_DATA_MISSING_SERVICES]
    service_list = hass.data[DOMAIN][HASS_DATA_PARSED_SERVICE_LIST]
    entities_missing = hass.data[DOMAIN][HASS_DATA_MISSING_ENTITIES]
    entity_list = hass.data[DOMAIN][HASS_DATA_PARSED_ENTITY_LIST]
    files_parsed = hass.data[DOMAIN][HASS_DATA_FILES_PARSED]
    files_ignored = hass.data[DOMAIN][HASS_DATA_FILES_IGNORED]
    chunk_size = (
        get_config(hass, CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)
        if chunk_size is None
        else chunk_size
    )

    rep = f"{header} \n"
    if services_missing:
        rep += f"\n-== Missing {len(services_missing)} service(s) from "
        rep += f"{len(service_list)} found in your config:\n"
        rep += render(hass, REPORT_ENTRY_TYPE_SERVICE)
        rep += "\n"
    elif len(service_list) > 0:
        rep += f"\n-== Congratulations, all {len(service_list)} services from "
        rep += "your config are available!\n"
    else:
        rep += "\n-== No services found in configuration files!\n"

    if entities_missing:
        rep += f"\n-== Missing {len(entities_missing)} entity(ies) from "
        rep += f"{len(entity_list)} found in your config:\n"
        rep += render(hass, REPORT_ENTRY_TYPE_ENTITY)
        rep += "\n"

    elif len(entity_list) > 0:
        rep += f"\n-== Congratulations, all {len(entity_list)} entities from "
        rep += "your config are available!\n"
    else:
        rep += "\n-== No entities found in configuration files!\n"

    def get_timezone(hass):
        return pytz.timezone(hass.config.time_zone)

    timezone = await hass.async_add_executor_job(get_timezone, hass)

    if not test_mode:
        report_datetime = datetime.now(timezone).strftime("%d %b %Y %H:%M:%S")
        parse_duration = hass.data[DOMAIN][HASS_DATA_PARSE_DURATION]
        check_duration = hass.data[DOMAIN][HASS_DATA_CHECK_DURATION]
        render_duration = time.time() - start_time
    else:
        report_datetime = "01 Jan 1970 00:00:00"
        parse_duration = 0.01
        check_duration = 0.105
        render_duration = 0.0003

    rep += f"\n-== Report created on {report_datetime}\n"
    rep += (
        f"-== Parsed {files_parsed} files in {parse_duration:.2f}s., "
        f"ignored {files_ignored} files \n"
    )
    rep += f"-== Generated in: {render_duration:.2f}s. Validated in: {check_duration:.2f}s."
    report_chunks = []
    chunk = ""
    for line in iter(rep.splitlines()):
        chunk += f"{line}\n"
        if chunk_size > 0 and len(chunk) > chunk_size:
            report_chunks.append(chunk)
            chunk = ""
    if chunk:
        report_chunks.append(chunk)
    return report_chunks
