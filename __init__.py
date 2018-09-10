# -*- coding: utf-8 -*-
"""PROXIMUS NTE (New Television Experience) user interface."""
import os
import re
import signal

from string import Template

from peewee import locales
from peewee.debug import GET_LOGGER, CHECK_LEAKS
from peewee.notifier import Task

from mal import input_plugins, MediaRoot

from pyskin import GLOBALS

from leon_mal.events import use_nte_events
from leon_mal.platform import get_platform, features

__all__ = ["main", "startup", "cleanup"]

log = GET_LOGGER(__name__)


platform = get_platform()  # 'v4', 'v5', 'mini-v5' or 'v6'.
features_cec = "cec" in features


def _init_input_plugins():
    # Must be called before any input_plugins loading!
    # Needed for Netflix.
    use_nte_events()

    # Activate optional keyboard plugin (for debug only).
    if os.getenv("KEYBOARD", "0").lower() in ("1", "y", "yes", "true"):
        input_plugins.init("keyboard")
        input_plugins.keyboard.THREADED = False
        input_plugins.keyboard.InputInterface.quit = lambda _: cleanup()

    if platform == "v4":
        input_plugin = "linux_event"
    elif platform == "v6":
        input_plugin = "fifo_event"
    else:  # v5 or mini-v5.
        input_plugin = "rf4ce_event"
    input_plugins.init(input_plugin)


def _init_internationalization():
    if platform == "v6":
        locales.LOCALE_DIR = "/mnt/nte/share/locale/"
    locales.init(install_gettext=False)


def start_boot_sequence():
    # The function is defined only so it can be mocked in unit tests
    # to be able to use the startup() method without starting the all
    # boot sequence

    # do not import BootSequence before profile is set -> local import
    from adele_app.boot_sequence import BootSequence
    Task(BootSequence).start()


def startup():
    """Start NTE application.

    Initialize the view manager, start the notifier and launch the
    boot sequence.
    """
    from leon_mal import setup_profile

    # FIXME: use smaller function to isolate each init block and
    # check where each import should be done!
    log.info("Starting up NTE application...")

    # Sanity check: platform value computed in app must be consistent
    # with the computed when generated the skin with skingen!
    # For now, skin value is only defined on v4 (skin is forked), so
    # value for other platforms (v5/v5c/v6) will be ``None``.
    skin_platform = GLOBALS.get("platform")
    v4_skin_platform = skin_platform == "v4"
    v4_platform = platform == "v4"
    if ((v4_platform and not v4_skin_platform) or
            (v4_skin_platform and not v4_platform)):
        # This case should never happen
        log.critical("\n\n!!! Configuration inconsistency !!!\n"
                     "App platform: %r\nSkin platform: %r\n"
                     "Fix this immediately.\n\n",
                     platform, skin_platform)
        return

    # Needed by Cds join / MultiSelect metadata (approved by fab).
    Template.pattern = re.compile(
        Template.pattern.pattern.replace("_a-z0-9", "_a-z/0-9"),
        re.IGNORECASE | re.VERBOSE)

    _init_input_plugins()
    _init_internationalization()
    setup_profile()

    if features_cec:
        # Specific CEC initialization
        from leon_mal.services.cec import CECManager
        # Initialize CEC
        log.info("Initialize CEC service")
        MediaRoot().update_services(cec=CECManager)

    start_boot_sequence()

    from wuk import application
    from wydgets.engine import ViewManager
    application.stacks = ("background", "main", "popup", "top")
    application.engines.append(ViewManager())
    application.run()


def cleanup():
    """
    Stops the application and closes the render connection
    Removes all the pages from the ADK stack.
    Free graphic memory.

    """
    from wuk import application
    log.warning("Entering into the cleanup process.")

    def cleanup_callback(_):
        # destroy all the alive Pages and their associated data
        application.destroy()

        # Stop services
        from adele_app.services import Services
        Services().stop()

        # force to free memory after graphics clean
        import gc
        gc.collect()

    # closing connection with media renderer
    from leon_mal.player import BasePlayer
    BasePlayer().close_renderer_connection(callback=cleanup_callback)

    from leon_mal.player import TVPlayer
    player = TVPlayer()
    if player.current_program is not None:
        try:
            player.current_program.clean()
        except AttributeError:
            # Killing the UI when listening to the radio (ProgramItem).
            pass


def main():
    # Try to import the mem_stats module: if available, we periodically
    # monitor several objects counts (all python objects, wyvas graphic objects,
    # open data sources ...) and start a WyDbus server so that the stats are
    # available on dbus.
    try:
        from adele_app import mem_stats
    except ImportError, e:
        log.warning("Cannot import the mem_stats module: %s", e.message)
    else:
        # The mem_stats module is present, let's monitor memory
        if CHECK_LEAKS:
            # instrument the application as soon as possible to track
            # instances of several classes using weak refs
            mem_stats.instrument_application()
        # Start the mem_stats C WyDbus server
        error = mem_stats.start_dbus_server()
        if not error:
            # The WyDbus server was successfully started, let's stop it at exit
            import atexit
            atexit.register(mem_stats.stop_dbus_server)
        else:
            # Failed to start the mem_stats WyDbus server
            log.warning("mem_stats.start_dbus_server() failed: %s")
    from logging import DEBUG
    from peewee.debug import log_threshold

    def exit_handler(signum, frame):
        log.warning("Exiting from application (signal %r) ...", signum)
        if frame and log_threshold <= DEBUG:
            from traceback import format_stack
            log.debug("Frame:\n%s", ''.join(format_stack(frame)))
        cleanup()

    # Use 'signal.SIGINT', otherwise the Ctrl+C exception will be
    # caught by the scheduler.
    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)

    startup()


if __name__ == "__main__":
    main()
