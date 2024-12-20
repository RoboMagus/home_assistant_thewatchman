"""https://github.com/dummylabs/thewatchman§"""

from datetime import timedelta
import logging
import time
import json
import voluptuous as vol
from anyio import Path
from homeassistant.helpers import config_validation as cv
from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_SERVICE_REGISTERED,
    EVENT_SERVICE_REMOVED,
    EVENT_STATE_CHANGED,
    EVENT_CALL_SERVICE,
)

from .coordinator import WatchmanCoordinator

from .utils import (
    is_service,
    report,
    parse,
    table_renderer,
    text_renderer,
    get_config,
    async_get_report_path,
)

from .const import (
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_HEADER,
    CONF_IGNORED_FILES,
    CONF_HEADER,
    CONF_REPORT_PATH,
    CONF_IGNORED_ITEMS,
    CONF_SERVICE_NAME,
    CONF_SERVICE_DATA,
    CONF_SERVICE_DATA2,
    CONF_INCLUDED_FOLDERS,
    CONF_CHECK_LOVELACE,
    CONF_IGNORED_STATES,
    CONF_CHUNK_SIZE,
    CONF_CREATE_FILE,
    CONF_SEND_NOTIFICATION,
    CONF_PARSE_CONFIG,
    CONF_COLUMNS_WIDTH,
    CONF_STARTUP_DELAY,
    CONF_FRIENDLY_NAMES,
    CONF_ALLOWED_SERVICE_PARAMS,
    CONF_TEST_MODE,
    EVENT_AUTOMATION_RELOADED,
    EVENT_SCENE_RELOADED,
    HASS_DATA_CANCEL_HANDLERS,
    HASS_DATA_COORDINATOR,
    HASS_DATA_FILES_IGNORED,
    HASS_DATA_FILES_PARSED,
    HASS_DATA_PARSE_DURATION,
    HASS_DATA_PARSED_ENTITY_LIST,
    HASS_DATA_PARSED_SERVICE_LIST,
    TRACKED_EVENT_DOMAINS,
    MONITORED_STATES,
    PLATFORMS,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_REPORT_PATH): cv.string,
                vol.Optional(CONF_IGNORED_FILES): cv.ensure_list,
                vol.Optional(CONF_IGNORED_ITEMS): cv.ensure_list,
                vol.Optional(CONF_HEADER, default=DEFAULT_HEADER): cv.string,
                vol.Optional(CONF_SERVICE_NAME): cv.string,
                vol.Optional(CONF_SERVICE_DATA): vol.Schema({}, extra=vol.ALLOW_EXTRA),
                vol.Optional(CONF_INCLUDED_FOLDERS): cv.ensure_list,
                vol.Optional(CONF_CHECK_LOVELACE, default=False): cv.boolean,
                vol.Optional(CONF_CHUNK_SIZE, default=3500): cv.positive_int,
                vol.Optional(CONF_IGNORED_STATES): [
                    "missing",
                    "unavailable",
                    "unknown",
                ],
                vol.Optional(CONF_COLUMNS_WIDTH): cv.ensure_list,
                vol.Optional(CONF_STARTUP_DELAY, default=0): cv.positive_int,
                vol.Optional(CONF_FRIENDLY_NAMES, default=False): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up is called when Home Assistant is loading our component."""
    if config.get(DOMAIN) is None:
        # We get here if the integration is set up using config flow
        return True

    hass.data.setdefault(DOMAIN_DATA, config[DOMAIN])
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=hass.data[DOMAIN_DATA]
        )
    )
    # Return boolean to indicate that initialization was successful.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI"""
    _LOGGER.debug(entry.options)
    _LOGGER.debug("Home assistant path: %s", hass.config.path(""))

    coordinator = WatchmanCoordinator(hass, _LOGGER, name=entry.title)
    coordinator.async_set_updated_data(None)
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.data[DOMAIN][HASS_DATA_COORDINATOR] = coordinator
    hass.data[DOMAIN_DATA] = entry.options  # TODO: refactor

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))
    await add_services(hass)
    await add_event_handlers(hass)
    if hass.is_running:
        # integration reloaded or options changed via UI
        await parse_config(hass, reason="changes in watchman configuration")
        await coordinator.async_config_entry_first_refresh()
    else:
        # first run, home assistant is loading
        # parse_config will be scheduled once HA is fully loaded
        _LOGGER.info("Watchman started [%s]", VERSION)
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Reload integration when options changed"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, config_entry):  # pylint: disable=unused-argument
    """Handle integration unload"""
    for cancel_handle in hass.data[DOMAIN].get(HASS_DATA_CANCEL_HANDLERS, []):
        if cancel_handle:
            cancel_handle()

    if hass.services.has_service(DOMAIN, "report"):
        hass.services.async_remove(DOMAIN, "report")

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if DOMAIN_DATA in hass.data:
        hass.data.pop(DOMAIN_DATA)
    if DOMAIN in hass.data:
        hass.data.pop(DOMAIN)

    if unload_ok:
        _LOGGER.info("Watchman integration successfully unloaded.")
    else:
        _LOGGER.error("Having trouble unloading watchman integration")

    return unload_ok


async def add_services(hass: HomeAssistant):
    """adds report service"""

    async def async_handle_report(call):
        """Handle the service call"""
        config = hass.data.get(DOMAIN_DATA, {})
        path = await async_get_report_path(hass, config.get(CONF_REPORT_PATH, None))
        send_notification = call.data.get(CONF_SEND_NOTIFICATION, False)
        create_file = call.data.get(CONF_CREATE_FILE, True)
        test_mode = call.data.get(CONF_TEST_MODE, False)
        # validate service params
        for param in call.data:
            if param not in CONF_ALLOWED_SERVICE_PARAMS:
                await async_notification(
                    hass,
                    "Watchman error",
                    f"Unknown service " f"parameter: `{param}`.",
                    error=True,
                )

        if not (send_notification or create_file):
            message = (
                "Either `send_notification` or `create_file` should be set to `true` "
                "in service parameters."
            )
            await async_notification(hass, "Watchman error", message, error=True)

        if call.data.get(CONF_PARSE_CONFIG, False):
            await parse_config(hass, reason="service call")

        if send_notification:
            chunk_size = call.data.get(CONF_CHUNK_SIZE, config.get(CONF_CHUNK_SIZE))
            service = call.data.get(CONF_SERVICE_NAME, None)
            service_data = call.data.get(CONF_SERVICE_DATA, None)

            if service_data and not service:
                await async_notification(
                    hass,
                    "Watchman error",
                    "Missing `service` parameter. The `data` parameter can only be used "
                    "in conjunction with `service` parameter.",
                    error=True,
                )

            if await async_onboarding(hass, service, path):
                await async_notification(
                    hass,
                    "🖖 Achievement unlocked: first report!",
                    f"Your first watchman report was stored in `{path}` \n\n "
                    "TIP: set `service` parameter in configuration.yaml file to "
                    "receive report via notification service of choice. \n\n "
                    "This is one-time message, it will not bother you in the future.",
                )
            else:
                await async_report_to_notification(
                    hass, service, service_data, chunk_size
                )

        if create_file:
            try:
                await async_report_to_file(hass, path, test_mode=test_mode)
            except OSError as exception:
                await async_notification(
                    hass,
                    "Watchman error",
                    f"Unable to write report: {exception}",
                    error=True,
                )

    hass.services.async_register(DOMAIN, "report", async_handle_report)


async def add_event_handlers(hass: HomeAssistant):
    """add event handlers"""

    async def async_schedule_refresh_states(hass, delay):
        """schedule refresh of the sensors state"""
        now = dt_util.utcnow()
        next_interval = now + timedelta(seconds=delay)
        async_track_point_in_utc_time(hass, async_delayed_refresh_states, next_interval)

    async def async_delayed_refresh_states(timedate):  # pylint: disable=unused-argument
        """refresh sensors state"""
        # parse_config should be invoked beforehand
        coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
        await coordinator.async_refresh()

    async def async_on_home_assistant_started(event):  # pylint: disable=unused-argument
        await parse_config(hass, reason="HA restart")
        startup_delay = get_config(hass, CONF_STARTUP_DELAY, 0)
        await async_schedule_refresh_states(hass, startup_delay)

    async def async_on_configuration_changed(event):
        typ = event.event_type
        if typ == EVENT_CALL_SERVICE:
            domain = event.data.get("domain", None)
            service = event.data.get("service", None)
            if domain in TRACKED_EVENT_DOMAINS and service in [
                "reload_core_config",
                "reload",
            ]:
                await parse_config(hass, reason="configuration changes")
                coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
                await coordinator.async_refresh()

        elif typ in [EVENT_AUTOMATION_RELOADED, EVENT_SCENE_RELOADED]:
            await parse_config(hass, reason="configuration changes")
            coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
            await coordinator.async_refresh()

    async def async_on_service_changed(event):
        service = f"{event.data['domain']}.{event.data['service']}"
        if service in hass.data[DOMAIN].get(HASS_DATA_PARSED_SERVICE_LIST, []):
            _LOGGER.debug("Monitored service changed: %s", service)
            coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
            await coordinator.async_refresh()

    async def async_on_state_changed(event):
        """refresh monitored entities on state change"""

        def state_or_missing(state_id):
            """return missing state if entity not found"""
            return "missing" if not event.data[state_id] else event.data[state_id].state

        if event.data["entity_id"] in hass.data[DOMAIN].get(
            HASS_DATA_PARSED_ENTITY_LIST, []
        ):
            ignored_states = get_config(hass, CONF_IGNORED_STATES, [])
            old_state = state_or_missing("old_state")
            new_state = state_or_missing("new_state")
            checked_states = set(MONITORED_STATES) - set(ignored_states)
            if new_state in checked_states or old_state in checked_states:
                _LOGGER.debug("Monitored entity changed: %s", event.data["entity_id"])
                coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
                await coordinator.async_refresh()

    # hass is not started yet, schedule config parsing once it loaded
    if not hass.is_running:
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, async_on_home_assistant_started
        )

    hdlr = []
    hdlr.append(
        hass.bus.async_listen(EVENT_CALL_SERVICE, async_on_configuration_changed)
    )
    hdlr.append(
        hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, async_on_configuration_changed)
    )
    hdlr.append(
        hass.bus.async_listen(EVENT_SCENE_RELOADED, async_on_configuration_changed)
    )
    hdlr.append(
        hass.bus.async_listen(EVENT_SERVICE_REGISTERED, async_on_service_changed)
    )
    hdlr.append(hass.bus.async_listen(EVENT_SERVICE_REMOVED, async_on_service_changed))
    hdlr.append(hass.bus.async_listen(EVENT_STATE_CHANGED, async_on_state_changed))
    hass.data[DOMAIN][HASS_DATA_CANCEL_HANDLERS] = hdlr


async def parse_config(hass: HomeAssistant, reason=None):
    """parse home assistant configuration files"""
    assert hass.data.get(DOMAIN_DATA)
    start_time = time.time()
    included_folders = get_included_folders(hass)
    ignored_files = hass.data[DOMAIN_DATA].get(CONF_IGNORED_FILES, None)

    parsed_entity_list, parsed_service_list, files_parsed, files_ignored = await parse(
        hass, included_folders, ignored_files, hass.config.config_dir
    )
    hass.data[DOMAIN][HASS_DATA_PARSED_ENTITY_LIST] = parsed_entity_list
    hass.data[DOMAIN][HASS_DATA_PARSED_SERVICE_LIST] = parsed_service_list
    hass.data[DOMAIN][HASS_DATA_FILES_PARSED] = files_parsed
    hass.data[DOMAIN][HASS_DATA_FILES_IGNORED] = files_ignored
    hass.data[DOMAIN][HASS_DATA_PARSE_DURATION] = time.time() - start_time
    _LOGGER.info(
        "%s files parsed and %s files ignored in %.2fs. due to %s",
        files_parsed,
        files_ignored,
        hass.data[DOMAIN][HASS_DATA_PARSE_DURATION],
        reason,
    )


def get_included_folders(hass):
    """gather the list of folders to parse"""
    folders = []
    config_folders = [hass.config.config_dir]

    if DOMAIN_DATA in hass.data:
        config_folders = hass.data[DOMAIN_DATA].get("included_folders")
        if not config_folders:
            config_folders = [hass.config.config_dir]

    for fld in config_folders:
        folders.append((fld, "**/*.yaml"))

    if DOMAIN_DATA in hass.data and hass.data[DOMAIN_DATA].get(CONF_CHECK_LOVELACE):
        folders.append((hass.config.config_dir, ".storage/**/lovelace*"))

    return folders


async def async_report_to_file(hass, path, test_mode):
    """save report to a file"""
    coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
    await coordinator.async_refresh()
    report_chunks = await report(
        hass, table_renderer, chunk_size=0, test_mode=test_mode
    )

    def write(path):
        with open(path, "w", encoding="utf-8") as report_file:
            for chunk in report_chunks:
                report_file.write(chunk)

    await hass.async_add_executor_job(write, path)


async def async_report_to_notification(hass, service_str, service_data, chunk_size):
    """send report via notification service"""
    if not service_str:
        service_str = get_config(hass, CONF_SERVICE_NAME, None)
        service_data = get_config(hass, CONF_SERVICE_DATA2, None)

    if not service_str:
        await async_notification(
            hass,
            "Watchman Error",
            "You should specify `service` parameter (in integration options or as `service` "
            "parameter) in order to send report via notification",
        )
        return

    if not is_service(hass, service_str):
        await async_notification(
            hass,
            "Watchman Error",
            f"{service_str} is not a valid service for notification",
        )
    domain = service_str.split(".")[0]
    service = ".".join(service_str.split(".")[1:])

    data = {} if service_data is None else json.loads(service_data)

    coordinator = hass.data[DOMAIN][HASS_DATA_COORDINATOR]
    await coordinator.async_refresh()
    report_chunks = await report(hass, text_renderer, chunk_size)
    for chunk in report_chunks:
        data["message"] = chunk
        # blocking=True ensures execution order
        if not await hass.services.async_call(domain, service, data, blocking=True):
            _LOGGER.error(
                "Unable to call service %s.%s due to an error.", domain, service
            )
            break


async def async_notification(hass, title, message, error=False, n_id="watchman"):
    """Show a persistent notification"""
    persistent_notification.async_create(
        hass,
        message,
        title=title,
        notification_id=n_id,
    )
    if error:
        raise HomeAssistantError(message.replace("`", ""))


async def async_onboarding(hass, service, path):
    """check if the user runs report for the first time"""
    service = service or get_config(hass, CONF_SERVICE_NAME, None)
    return not (service or await Path(path).exists())
