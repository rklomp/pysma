"""SMA WebConnect library for Python.

See: http://www.sma.de/en/products/monitoring-control/webconnect.html

Source: http://www.github.com/kellerza/pysma
"""
import asyncio
import json
import logging

import async_timeout
import attr
import jmespath
from aiohttp import client_exceptions

from .const import (
    DEVICE_INFO,
    FALLBACK_DEVICE_INFO,
    JMESPATH_VAL,
    JMESPATH_VAL_IDX,
    SENSOR_MAP,
    URL_DASH_LOGGER,
    URL_DASH_VALUES,
    URL_LOGGER,
    URL_LOGIN,
    URL_LOGOUT,
    URL_VALUES,
    USERS,
)

_LOGGER = logging.getLogger(__name__)


@attr.s(slots=True)
class Sensor:
    """pysma sensor definition."""

    key = attr.ib()
    name = attr.ib()
    unit = attr.ib(default="")
    factor = attr.ib(default=None)
    path = attr.ib(default=None)
    enabled = attr.ib(default=True)
    l10n_translate = attr.ib(default=False)
    value = attr.ib(default=None, init=False)
    key_idx = attr.ib(default="0", repr=False, init=False)

    def __attrs_post_init__(self):
        """Init path."""
        idx = "0"
        key = str(self.key)
        if key[-2] == "_" and key[-1].isdigit():
            idx = key[-1]
            key = key[:-2]
        self.key = key
        self.key_idx = idx

    def extract_logger(self, result_body):
        """Extract logs from json body."""
        self.value = result_body

    def extract_value(self, result_body, l10n={}, devclass="1"):
        """Extract value from json body."""
        try:
            res = result_body[self.key]
        except (KeyError, TypeError):
            _LOGGER.warning("Sensor %s: Not found in %s", self.key, result_body)
            res = self.value
            self.value = None
            return self.value != res

        if not isinstance(self.path, str):
            # Try different methods until we can decode...
            _paths = (
                list(self.path)
                if isinstance(self.path, (list, tuple))
                else [
                    JMESPATH_VAL,
                    JMESPATH_VAL_IDX.format(devclass, self.key_idx),
                ]
            )

            while _paths:
                _path = _paths.pop()
                _val = jmespath.search(_path, res)
                if _val is not None:
                    _LOGGER.debug(
                        "Sensor %s: Will be decoded with %s from %s",
                        self.name,
                        _path,
                        res,
                    )
                    self.path = _path
                    break

        # Extract new value
        if isinstance(self.path, str):
            res = jmespath.search(self.path, res)
        else:
            _LOGGER.debug(
                "Sensor %s: No successful value decoded yet: %s", self.name, res
            )
            res = None

        if isinstance(res, (int, float)) and self.factor:
            res /= self.factor

        if self.l10n_translate:
            res = l10n.get(
                str(res),
                res,
            )

        try:
            return res != self.value
        finally:
            self.value = res


class Sensors:
    """SMA Sensors."""

    def __init__(self, sensors=None):
        self.__s = []

        if sensors:
            self.add(sensors)

    def __len__(self):
        """Length."""
        return len(self.__s)

    def __contains__(self, key):
        """Check if a sensor is defined."""
        try:
            if self[key]:
                return True
        except KeyError:
            return False

    def __getitem__(self, key):
        """Get a sensor using either the name or key."""
        for sen in self.__s:
            if sen.name == key or sen.key == key:
                return sen
        raise KeyError(key)

    def __iter__(self):
        """Iterator."""
        return self.__s.__iter__()

    def add(self, sensor):
        """Add a sensor, warning if it exists."""
        if isinstance(sensor, (list, tuple)):
            for sss in sensor:
                self.add(sss)
            return

        if isinstance(sensor, dict):
            self.add(Sensor(**sensor))
            return

        if not isinstance(sensor, Sensor):
            raise TypeError("pysma.Sensor expected")

        if sensor.name in self:
            old = self[sensor.name]
            self.__s.remove(old)
            _LOGGER.warning("Replacing sensor %s with %s", old, sensor)

        if sensor.key in self and self[sensor.key].key_idx == sensor.key_idx:
            _LOGGER.warning(
                "Duplicate SMA sensor key %s (idx: %s)", sensor.key, sensor.key_idx
            )

        self.__s.append(sensor)


class SMA:
    """Class to connect to the SMA webconnect module and read parameters."""

    def __init__(self, session, url, password=None, group="user", uid=None):
        """Init SMA connection."""
        # pylint: disable=too-many-arguments
        if group not in USERS:
            raise KeyError("Invalid user type: {}".format(group))
        if password is not None and len(password) > 12:
            _LOGGER.warning("Password should not exceed 12 characters")
        if password is None:
            self._new_session_data = None
        else:
            self._new_session_data = {"right": USERS[group], "pass": password}
        self._url = url.rstrip("/")
        if not url.startswith("http"):
            self._url = "http://" + self._url
        self._aio_session = session
        self.sma_sid = None
        self.sma_uid = uid
        self.l10n = {}
        self.devclass = None
        self.device_info_sensors = Sensors(SENSOR_MAP[DEVICE_INFO])

    async def _fetch_json(self, url, payload):
        """Fetch json data for requests."""
        params = {
            "data": json.dumps(payload),
            "headers": {"content-type": "application/json"},
            "params": {"sid": self.sma_sid} if self.sma_sid else None,
        }
        for _ in range(3):
            try:
                with async_timeout.timeout(10):
                    res = await self._aio_session.post(self._url + url, **params)
                    return (await res.json()) or {}
            except (asyncio.TimeoutError, client_exceptions.ClientError):
                continue
        return {"err": "Could not connect to SMA at {} (timeout)".format(self._url)}

    async def _read_l10n(self, lang="en-US"):
        """Read device language file."""
        res = await self._aio_session.get(f"{self._url}/data/l10n/{lang}.json")
        return (await res.json()) or {}

    async def _read_body(self, url, payload):
        if self.sma_sid is None and self._new_session_data is not None:
            await self.new_session()
            if self.sma_sid is None:
                return None
        body = await self._fetch_json(url, payload=payload)

        # On the first error we close the session which will re-login
        err = body.get("err")
        if err is not None:
            _LOGGER.warning(
                "%s: error detected, closing session to force another login attempt, got: %s",
                self._url,
                body,
            )
            await self.close_session()
            return None

        if not isinstance(body, dict) or "result" not in body:
            _LOGGER.warning("No 'result' in reply from SMA, got: %s", body)
            return None

        if self.sma_uid is None:
            # Get the unique ID
            self.sma_uid = next(iter(body["result"].keys()), None)

        result_body = body["result"].pop(self.sma_uid, None)
        if body != {"result": {}}:
            _LOGGER.warning(
                "Unexpected body %s, extracted %s",
                json.dumps(body),
                json.dumps(result_body),
            )

        return result_body

    async def new_session(self):
        """Establish a new session."""
        self.l10n = await self._read_l10n()
        body = await self._fetch_json(URL_LOGIN, self._new_session_data)
        self.sma_sid = jmespath.search("result.sid", body)
        if self.sma_sid:
            return True

        err = body.pop("err", None)
        msg = "Could not start session, %s, got {}".format(body)

        if err:
            if err == 503:
                _LOGGER.error(msg, "Max amount of sessions reached")
            else:
                _LOGGER.error(msg, err)
        else:
            _LOGGER.error(msg, "Session ID expected [result.sid]")
        return False

    async def close_session(self):
        """Close the session login."""
        if self.sma_sid is None:
            return
        try:
            await self._fetch_json(URL_LOGOUT, {})
        finally:
            self.sma_sid = None

    async def read(self, sensors):
        """Read a set of keys."""
        if self._new_session_data is None:
            payload = {"destDev": [], "keys": []}
            result_body = await self._read_body(URL_DASH_VALUES, payload)
        else:
            payload = {
                "destDev": [],
                "keys": list({s.key for s in sensors if s.enabled}),
            }
            result_body = await self._read_body(URL_VALUES, payload)
        if not result_body:
            return False

        notfound = []
        devclass = self.get_devclass(result_body)
        for sen in sensors:
            if sen.enabled:
                if sen.key in result_body:
                    sen.extract_value(result_body, self.l10n, devclass)
                    continue

                notfound.append(f"{sen.name} [{sen.key}]")

        if notfound:
            _LOGGER.warning(
                "No values for sensors: %s. Response from inverter: %s",
                ",".join(notfound),
                result_body,
            )

        return True

    async def read_logger(self, sensors=None, start=None, end=None):
        """Read a logging key and return the results."""
        if self._new_session_data is None:
            payload = {"destDev": [], "key": []}
            result_body = await self._read_body(URL_DASH_LOGGER, payload)
        else:
            payload = {
                "destDev": [],
                "key": int(sensors[0].key),
                "tStart": start,
                "tEnd": end,
            }
            result_body = await self._read_body(URL_LOGGER, payload)
        if not result_body:
            return False

        for sen in sensors:
            sen.extract_logger(result_body)

        return True

    async def device_info(self):
        """Read device info and return the results."""
        values = await self.read(self.device_info_sensors)
        if not values:
            return False

        device_info = {
            "serial": self.device_info_sensors["serial_number"].value
            or FALLBACK_DEVICE_INFO["serial"],
            "name": self.device_info_sensors["device_name"].value
            or FALLBACK_DEVICE_INFO["name"],
            "type": self.device_info_sensors["device_type"].value
            or FALLBACK_DEVICE_INFO["type"],
            "manufacturer": self.device_info_sensors["device_manufacturer"].value
            or FALLBACK_DEVICE_INFO["manufacturer"],
        }

        return device_info

    def get_devclass(self, result_body):
        if not self.devclass:
            sensor_values = list(result_body.values())
            if len(sensor_values) == 0:
                return None

            devclass_keys = list(sensor_values[0].keys())
            if len(devclass_keys) == 0:
                return None
            if len(devclass_keys) > 1:
                raise KeyError("More than 1 device class key is not supported")

            self.devclass = devclass_keys[0]

        return self.devclass

    # TODO: add check if devclass is set and retreive it somehow if not...
    async def get_sensors(self):
        return Sensors(SENSOR_MAP[self.devclass])
