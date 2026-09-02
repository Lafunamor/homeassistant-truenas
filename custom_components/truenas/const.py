"""Constants used by the TrueNAS integration."""

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.UPDATE,
]

DOMAIN = "truenas"
DEFAULT_NAME = "root"
ATTRIBUTION = "Data provided by TrueNAS integration"

# How many update cycles a reporting graph stays muted after it failed.
SYSTEMSTATS_RETRY_AFTER = 60

# System update API. TrueNAS 25.04 replaced update.check_available with
# update.status, and repurposed update.update to change the update settings
# rather than install an update - update.run installs one.
UPDATE_STATUS = "update.status"
UPDATE_RUN = "update.run"
LEGACY_UPDATE_CHECK = "update.check_available"
LEGACY_UPDATE_RUN = "update.update"

# Virtual machines and containers, which have moved twice. vm.* is the
# libvirt based API present in every release. TrueNAS 25.04 added the Incus
# based virt.instance.* alongside it; TrueNAS 26 removes the whole virt.*
# namespace again and exposes LXC containers as container.*. Any of them can
# hold instances, so all three are queried.
VM_API_VIRT = "virt"
VM_API_LEGACY = "vm"
VM_API_CONTAINER = "container"
VM_QUERY_VIRT = "virt.instance.query"
VM_QUERY_LEGACY = "vm.query"
VM_QUERY_CONTAINER = "container.query"

# Snapshot creation. zfs.snapshot.create, which the integration used to
# call, does not exist on any current TrueNAS.
SNAPSHOT_CREATE = "pool.snapshot.create"

# Service control. TrueNAS 26 removed service.start/stop/restart/reload in
# favour of a single service.control(VERB, service).
SERVICE_CONTROL = "service.control"

DEFAULT_HOST = "10.0.0.1"
DEFAULT_USERNAME = "admin"

DEFAULT_DEVICE_NAME = "TrueNAS"
DEFAULT_SSL = True
DEFAULT_SSL_VERIFY = False

TO_REDACT = {
    "username",
    "password",
    "encryption_password",
    "encryption_salt",
    "host",
    "api_key",
    "serial",
    "system_serial",
    "ip4_addr",
    "ip6_addr",
    "account",
    "key",
}

SERVICE_CLOUDSYNC_RUN = "cloudsync_run"
SCHEMA_SERVICE_CLOUDSYNC_RUN = {}

SERVICE_CLOUDSYNC_ABORT = "cloudsync_abort"
SCHEMA_SERVICE_CLOUDSYNC_ABORT = {}

SERVICE_DATASET_SNAPSHOT = "dataset_snapshot"
SCHEMA_SERVICE_DATASET_SNAPSHOT = {}

SERVICE_SYSTEM_REBOOT = "system_reboot"
SCHEMA_SERVICE_SYSTEM_REBOOT = {}

SERVICE_SYSTEM_SHUTDOWN = "system_shutdown"
SCHEMA_SERVICE_SYSTEM_SHUTDOWN = {}

SERVICE_SERVICE_START = "service_start"
SCHEMA_SERVICE_SERVICE_START = {}
SERVICE_SERVICE_STOP = "service_stop"
SCHEMA_SERVICE_SERVICE_STOP = {}
SERVICE_SERVICE_RESTART = "service_restart"
SCHEMA_SERVICE_SERVICE_RESTART = {}
SERVICE_SERVICE_RELOAD = "service_reload"
SCHEMA_SERVICE_SERVICE_RELOAD = {}

SERVICE_VM_START = "vm_start"
SERVICE_VM_START_OVERCOMMIT = "overcommit"
SCHEMA_SERVICE_VM_START = {vol.Optional(SERVICE_VM_START_OVERCOMMIT): cv.boolean}
SERVICE_VM_STOP = "vm_stop"
SCHEMA_SERVICE_VM_STOP = {}

SERVICE_APP_START = "app_start"
SCHEMA_SERVICE_APP_START = {}
SERVICE_APP_STOP = "app_stop"
SCHEMA_SERVICE_APP_STOP = {}
