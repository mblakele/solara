"""Configuration management via .env files and environment variables.

Forked from https://github.com/HBNetwork/python-decouple under the MIT License.
Copyright (c) 2013 Henrique Bastos.
"""

# coding: utf-8
import os
import sys
import string
from shlex import shlex
from collections import OrderedDict

# Python 3 only — configparser and read_file are standard since 3.2+
from configparser import ConfigParser, NoOptionError

text_type = str


def read_config(parser, file):
    """Read configuration from a file into the given parser."""
    return parser.read_file(file)


DEFAULT_ENCODING = 'UTF-8'


# Python 3.10 don't have strtobool anymore. So we move it here.
TRUE_VALUES = {"y", "yes", "t", "true", "on", "1"}
FALSE_VALUES = {"n", "no", "f", "false", "off", "0"}

def strtobool(value):
    """Convert a string representation of truth to True or False.

    Args:
        value: String or bool to convert.

    Returns:
        Boolean interpretation of the value.

    Raises:
        ValueError: If value is not a recognized truth value.
    """
    if isinstance(value, bool):
        return value
    value = value.lower()

    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False

    raise ValueError(f"Invalid truth value: {value}")


class UndefinedValueError(Exception):
    """Raised when a config key is missing and no default was provided."""


class Undefined:
    """Sentinel class to represent an undefined configuration value."""


# Reference instance to represent undefined values
undefined = Undefined()


class Config:
    """Handle .env file format used by Foreman."""

    def __init__(self, repository):
        self.repository = repository

    def _cast_boolean(self, value):
        """
        Helper to convert config values to boolean as ConfigParser do.
        """
        value = str(value)
        return bool(value) if value == '' else bool(strtobool(value))

    @staticmethod
    def _cast_do_nothing(value):
        return value

    def get(self, option, default=undefined, cast=undefined):
        """
        Return the value for option or default if defined.
        """

        # We can't avoid __contains__ because value may be empty.
        if option in os.environ:
            value = os.environ[option]
        elif option in self.repository:
            value = self.repository[option]
        else:
            if isinstance(default, Undefined):
                raise UndefinedValueError(
                    f"{option} not found. "
                    "Declare it as envvar or define a default value."
                )

            value = default

        if isinstance(cast, Undefined):
            cast = self._cast_do_nothing
        elif cast is bool:
            cast = self._cast_boolean

        return cast(value)

    def __call__(self, *args, **kwargs):
        """
        Convenient shortcut to get.
        """
        return self.get(*args, **kwargs)





class RepositoryEmpty:
    """Base repository with no data — used as a fallback."""

    def __init__(self, source='', encoding=DEFAULT_ENCODING):
        pass

    def __contains__(self, key):
        return False

    def __getitem__(self, key):
        return None

    def clear(self, key):
        """No-op — there is no data to clear."""

class RepositoryIni(RepositoryEmpty):
    """
    Retrieves option keys from .ini files.
    """
    SECTION = 'settings'

    def __init__(self, source, encoding=DEFAULT_ENCODING):
        self.parser = ConfigParser()
        with open(source, encoding=encoding) as file_:
            read_config(self.parser, file_)

    def __contains__(self, key):
        return (key in os.environ or
                self.parser.has_option(self.SECTION, key))

    def __getitem__(self, key):
        try:
            return self.parser.get(self.SECTION, key)
        except NoOptionError as exc:
            raise KeyError(key) from exc

    def clear(self, key):
        self.parser.remove_option(self.SECTION, key)


class RepositoryEnv(RepositoryEmpty):
    """
    Retrieves option keys from .env files with fall back to os.environ.
    """
    def __init__(self, source, encoding=DEFAULT_ENCODING):
        self.data = {}

        with open(source, encoding=encoding) as file_:
            for line in file_:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and ((v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')):
                    v = v[1:-1]
                self.data[k] = v

    def __contains__(self, key):
        return key in os.environ or key in self.data

    def __getitem__(self, key):
        return self.data[key]

    def clear(self, key):
        self.data.pop(key, None)


class RepositorySecret(RepositoryEmpty):
    """
    Retrieves option keys from files,
    where title of file is a key, content of file is a value
    e.g. Docker swarm secrets
    """

    def __init__(self, source='/run/secrets/'):
        self.data = {}

        ls = os.listdir(source)
        for file in ls:
            with open(os.path.join(source, file), encoding=DEFAULT_ENCODING) as f:
                self.data[file] = f.read()

    def __contains__(self, key):
        return key in os.environ or key in self.data

    def __getitem__(self, key):
        return self.data[key]

    def clear(self, key):
        self.data.pop(key, None)


class AutoConfig:
    """Autodetects the config file and type.

    Parameters
    ----------
    search_path : str, optional
        Initial search path. If empty, the default search path is the
        caller's path.
    """
    SUPPORTED = OrderedDict([
        ('settings.ini', RepositoryIni),
        ('.env', RepositoryEnv),
    ])

    encoding = DEFAULT_ENCODING

    def __init__(self, search_path=None):
        self.search_path = search_path
        self.config = None

    def _find_file(self, path):
        # look for all files in the current path
        for configfile in self.SUPPORTED:
            filename = os.path.join(path, configfile)
            if os.path.isfile(filename):
                return filename

        # search the parent
        parent = os.path.dirname(path)
        if parent and os.path.normcase(parent) != os.path.normcase(os.path.abspath(os.sep)):
            return self._find_file(parent)

        # reached root without finding any files.
        return ''

    def _load(self, path):
        # Avoid unintended permission errors
        try:
            filename = self._find_file(os.path.abspath(path))
        except Exception:
            filename = ''
        Repository = self.SUPPORTED.get(os.path.basename(filename), RepositoryEmpty)

        self.config = Config(Repository(filename, encoding=self.encoding))

    def _caller_path(self):
        # MAGIC! Get the caller's module path.
        frame = sys._getframe()  # pylint: disable=protected-access
        path = os.path.dirname(frame.f_back.f_back.f_code.co_filename)
        return path

    def __call__(self, *args, **kwargs):
        if not self.config:
            self._load(self.search_path or self._caller_path())

        return self.config(*args, **kwargs)

    def _ensure_loaded(self):
        if not self.config:
            self._load(self.search_path or self._caller_path())

    def get_all(self) -> dict[str, str]:
        """Return all configuration values from both .env/settings.ini and os.environ.

        Merges parsed config file data with os.environ, where os.environ
        takes precedence for overlapping keys.

        Returns:
            Dict of all config key-value pairs as strings.
        """
        self._ensure_loaded()
        merged: dict[str, str] = {}
        # Not all repository types expose a .data dict (e.g. RepositoryIni uses
        # ConfigParser internally), so guard before iterating.
        repo = self.config.repository
        if hasattr(repo, 'data'):
            merged = {k: str(v) for k, v in repo.data.items()}
        # os.environ takes precedence, matching decouple's own lookup order.
        merged.update(os.environ)
        return merged

    def set(self, option, value):
        """Set a config value in os.environ (takes precedence over .env file)."""
        os.environ[option] = value

    def clear(self, option):
        """Remove a config value from os.environ (does not modify the .env file)."""
        os.environ.pop(option, None)
        self.config.repository.clear(option)

    def clear_all(self):
        """Remove all known config keys from os.environ."""
        for key in list(self.get_all().keys()):
            self.clear(key)


# A pre-instantiated AutoConfig to improve decouple's usability
# now just import config and start using with no configuration.
config = AutoConfig()

# Helpers

class Csv:
    """Produce a CSV parser that returns a list of transformed elements."""

    def __init__(self, cast=text_type, delimiter=',', strip=string.whitespace, post_process=list):
        """Initialize the CSV parser.

        Parameters:
        cast -- callable that transforms each item before adding to the list.
        delimiter -- string of delimiter characters passed to shlex.
        strip -- string of characters to strip from each item after splitting.
        post_process -- callable applied to all casted values. Default is `list`.
        """
        self.cast = cast
        self.delimiter = delimiter
        self.strip = strip
        self.post_process = post_process

    def __call__(self, value):
        """The actual transformation"""
        if value is None:
            return self.post_process()

        def transform(s):
            return self.cast(s.strip(self.strip))

        splitter = shlex(value, posix=True)
        splitter.whitespace = self.delimiter
        splitter.whitespace_split = True

        return self.post_process(transform(s) for s in splitter)


class Choices:
    """Cast and validate values against a list of allowed choices."""

    def __init__(self, flat=None, cast=text_type, choices=None):
        """Initialize the choices validator.

        Parameters:
        flat -- a flat list of valid choices.
        cast -- callable that transforms value before validation.
        choices -- tuple of Django-like choices.
        """
        self.flat = flat or []
        self.cast = cast
        self.choices = choices or []

        self._valid_values = []
        self._valid_values.extend(self.flat)
        self._valid_values.extend([value for value, _ in self.choices])

    def __call__(self, value):
        transform = self.cast(value)
        if transform not in self._valid_values:
            raise ValueError(
                f"Value not in list: {value!r}; valid values are {self._valid_values!r}"
            )
        return transform
