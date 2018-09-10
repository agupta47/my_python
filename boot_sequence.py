# -*- encoding: utf-8 -*-
"""
Boot sequence
"""

import os

from peewee.debug import GET_LOGGER
from peewee.notifier import Task
from peewee.misc_utils import MetaSingleton

import wuk
from wuk.pages.stack_utils import PageNotFoundError

from mal import MediaRoot, profile

from leon_mal.avr import AVR
from leon_mal.lang import (get_locale_language, set_locale_language,
                           get_config_language, set_config_language)
from leon_mal.player import BasePlayer, TVPlayer
from leon_mal.restriction_management import ParentalControl
from leon_mal.services.ocid import OCIDService, PARSED
from leon_mal.errors.error_codes import (
    STB_IS_NOT_ASSIGNED_TRIGGERS, get_error_code)
from leon_mal.errors.service import ErrorProxyService

from leon_app_tools.fpdisplay_tools import FrontPanelTools
from leon_app_tools.return_codes import (
    TRY_AGAIN, STB_ASSIGNED,
    SW_UPDATE_NOT_READY, NO_SW_UPDATE,
    SW_UPDATE_DOWNLOAD, SW_UPDATE_DOWNLOAD_IN_PROGRESS,
    SW_UPDATE_DOWNLOAD_FAILED, OCI_NETWORK_TIMEOUT,
    SW_UPDATE_NAND_WRITE, SW_UPDATE_NAND_WRITE_FAILED)

from adele_app.facilities.poster_servers import set_poster_servers
from adele_app.services import Services
from adele_app.pages.boot.boot_page import BootPage
from adele_app.pages.error.boot_error import ErrorPage
from adele_app.pages.error.sw_update_download import SWUpdateDownloadPage
from adele_app.tools.error_cluster_management import ErrorClusterManager
from leon_mal.services.btapi import BTAPIService, SHOW_CASE_ON_GOING, SHOW_CASE_OK
from adele_app.stand_by import is_passive_standby_enabled

from leon_mal.platform import platform


if platform == "v6":
    from leon_mal.stubs import generate_mock
    save_stb_name = generate_mock("save_stb_name")
else:
    from leon_mal.services.nxlauncher import save_stb_name


log = GET_LOGGER(__name__)

SW_UPDATE_ACTIVE = (SW_UPDATE_DOWNLOAD,
                    SW_UPDATE_DOWNLOAD_IN_PROGRESS,
                    SW_UPDATE_NAND_WRITE)
SW_UPDATE_FAILURE = (SW_UPDATE_NAND_WRITE_FAILED, SW_UPDATE_DOWNLOAD_FAILED)
STATUS_NOT_READY = list(TRY_AGAIN) + [STB_ASSIGNED, OCI_NETWORK_TIMEOUT]


def wait_dr_repair_check(f):
    import time
    from wydbus import WyDbus
    from com import dbus

    # This was done normaly by the initng
    # But we need to boot even if ocid is not here
    # To avoid missleading information in doctor repair
    # wait a bit to let ocid boot
    dbus = dbus(bus=WyDbus(), name="org.freedesktop.DBus")

    def try_to_wait_ocid(cpt=6):
        if not cpt:
            return

        if not dbus.NameHasOwner("com.wyplay.oci"):
            log.info("DrRepair :: Trying to wait for OCID")
            time.sleep(0.5)
            try_to_wait_ocid(cpt - 1)

    def launch_dr_repair(*args, **kwargs):

        try_to_wait_ocid()

        # Get/set language for Self doctor Repair trads
        # when booting without network
        lang = get_config_language()
        log.debug("BOOT: language: %s", lang)
        if lang != get_locale_language():
            set_locale_language(lang)

        from adele_app.pages.dr_repair.doctor_repair_page import (
            DoctorRepairPage, DR_LAUNCH_FROM_BOOT)
        page = DoctorRepairPage(DR_LAUNCH_FROM_BOOT)
        on_remove = page.on_remove

        def keep_going():
            on_remove()
            f(*args, **kwargs)

        page.on_remove = keep_going
        page.show()

    return launch_dr_repair


class BootSequence(object):

    __metaclass__ = MetaSingleton

    def __init__(self):
        self.first_page = 'main_hub'
        self.send_avr = False
        self._is_signal_connected = False
        # This argument tells the "boot sequence" to save the PBU lang
        # passed to the backend once the boot is finished.
        # This is used after first install.
        self.save_pbu_lang = None
        self._sw_update_failure_cb = None
        self.is_migration = None
        self._retrieve_migration_status()
        self._init_front_panel()
        self.start()

    def _retrieve_migration_status(self):
        migration = MediaRoot().get_service("migration")
        try:
            migration_status = migration.get_migration_status()
        except RuntimeError, e:
            # XXX: why is this try/except not done in service?
            log.error("Migration error: %r", e)
            log.warning("Migration status will be ignored")
            migration_status = "NO_MIGRATION"
        else:
            # TODO: add comment to explain why this is needed!
            from leon_mal.userconfigmgmt import UserConfigMgmt
            UserConfigMgmt.flush_cache()
        self.is_migration = migration_status == "MIGRATION_DONE"
        log.info("Migration status is %s.", migration_status)

    def _init_front_panel(self):
        log.info("Initialize front panel.")
        FrontPanelTools().restore_front_panel()

    def hide_splashscreen(self):
        try:
            wuk.application.get('BootPage').hide()
        except Exception, e:
            log.warning("Failed to close BootPage: %r", e)

    def show_splashscreen(self):
        """ show boot page on top of stack (move on top if already
        in stack; else instanciate and show)
        """
        try:
            bootpage = wuk.application.get('BootPage')
            wuk.application.move_on_top(bootpage)
        except PageNotFoundError:
            bootpage = BootPage()
            bootpage.show()

    def _start(self):
        log.info("[BOOT] start")

        # launch the error manager
        ErrorClusterManager()
        BootPage().show()

        Task(self.loop_check_middleware()).start(delay=3, loop=True,
                                                 init_delay=1)

    def start(self):
        """
        Start Node:

        Show the splash screen.
        Initialize the components that are not dependent to the
        middleware.
        """
        wait_dr_repair_check(self._start)()

    def loop_check_middleware(self):
        """Middleware components start Node.

        Wait for the middleware components to start and be visible on
        DBUS.
        """
        log.info("[BOOT] loop_check_middleware")
        # FIXME: Must check all providers. But some providers are not
        # present on Adele target.
        if platform == "v6":
            unused_provider = ("storage", "teletext", "nxlauncher",
                               "cpc", "webbrowser", "avio")
        else:
            unused_provider = ("storage", "teletext", "nxlauncher",
                               "cpc", "webbrowser")

        max_tries = 20 * 10  # (3 x 20 = 60 sec) x 10 = 10 min
        for i in range(max_tries):

            for middleware in profile.selected['providers'].keys():
                if (not MediaRoot().is_provider_connected(middleware) and
                        middleware not in unused_provider):
                    log.info("Middleware provider %r is not connected.",
                             middleware)
                    break
            else:
                # Start UI services
                Services().start()

                # Launch the connection to the renderer
                def renderer_started(request):
                    if request.is_failed():
                        # FIXME: Remove this custom error message.
                        # Must be replaced by a charted error
                        ErrorPage(
                            title=_("FAILED TO LAUNCH THE TV PLAYER"),
                            description=_("Impossible to launch the TV player."),
                            error_message=_("Error code=%r" %
                                            request.return_code)).show()
                        self.hide_splashscreen()
                        return

                    Task(self.loop_check_alternative_flow()).start(
                        delay=3, loop=True, init_delay=0.1)

                TVPlayer().start_renderer(callback=renderer_started)
                break

            yield  # RETRY

        else:
            # FIXME: Remove this custom error message.
            ErrorPage(
                title="Middleware Timeout Error",
                description="Unable to start the middleware.",
                error_message="10 min timeout reached. Please restart.").show()
            self.hide_splashscreen()

    def loop_check_alternative_flow(self):
        """Alternative boot flow check Node.

        Check if the boot must compete normally or not.
        Alternative boot flows are:
         - Software update
         - First install
        """
        log.info("[BOOT] loop_check_alternative_flow")
        max_tries = 20 * 10  # (3 x 20 = 60 sec) x 10 = 10 min
        ocid = OCIDService()
        for i in range(max_tries):
            try:
                update_status = ocid.sw_update_state_get()
                assign_status = ocid.boot_status_get()
            except RuntimeError:
                # We just lost OCID, probably because of a
                # SIGSEGV or SIGBUS, we need to wait for it
                # to come back on the bus
                #  -> Try again
                log.error("OCID is not on DBus... try again")
                yield

            if update_status in SW_UPDATE_ACTIVE:
                # Alternative flow: Show software update Root page
                log.info("A software update is currently in progress: "
                         "update status = %s", update_status)

                sw_update_page = SWUpdateDownloadPage(
                    title=_("[SW_UPDATE]SOFTWARE DOWNLOAD"),
                    text=_("[SW_UPDATE]Please wait during loading.\n"
                           "Your box will start again at the end"))

                def _handle_sw_update_failure(*args, **kwargs):
                    """Resume normal boot if an error occured with the
                    software update.
                    """
                    log.error("A problem occured during software update: "
                              "resume regular boot sequence...")
                    BootPage().show()
                    sw_update_page.hide()

                    if self._sw_update_failure_cb is not None:
                        ocid.unregister(self._sw_update_failure_cb)
                        self._sw_update_failure_cb = None

                    Task(self.loop_check_alternative_flow()).start(
                        delay=3, loop=True, init_delay=0.1)

                self._sw_update_failure_cb = _handle_sw_update_failure
                ocid.register(
                    self._sw_update_failure_cb,
                    sw_update_state_update="SW_UPDATE_DOWNLOAD_FAILED")
                ocid.register(
                    self._sw_update_failure_cb,
                    sw_update_state_update="SW_UPDATE_NAND_WRITE_FAILED")

                sw_update_page.show()
                self.hide_splashscreen()
                break

            elif assign_status in STB_IS_NOT_ASSIGNED_TRIGGERS:
                # Alternative flow: Show first install Root page

                self.first_page = 'pin_code_change'
                from adele_app.pages.first_install.neutral_language import \
                    NeutralLanguage
                NeutralLanguage().show()

                if assign_status == get_error_code("TM_INACTIVE_ACCOUNT"):
                    # go to error cluster page...and be blocked until account
                    # is activated again (need a reboot)
                    from peewee.request import Request
                    req = Request()
                    req.start()
                    req.fail(assign_status)
                    ErrorProxyService().caught(req)

                break

            elif ((update_status == NO_SW_UPDATE or
                    update_status in SW_UPDATE_FAILURE) and
                    assign_status == STB_ASSIGNED):
                Task(self.loop_check_document_parsing).start()
                break

            elif (update_status in (SW_UPDATE_NOT_READY, NO_SW_UPDATE) and
                  assign_status in STATUS_NOT_READY):
                log.info("SW update=%r or Boot status=%r not ready.",
                         update_status, assign_status)
                yield  # RETRY

            else:
                log.info("SW update=%r or Boot status=%r not ready.",
                         update_status, assign_status)
                from peewee.request import Request
                req = Request()
                req.start()
                req.fail(assign_status)
                ErrorProxyService().caught(req)
                self.hide_splashscreen()
                break
        else:
            status_message = ("SW update status:%r, Boot status:%r." %
                              (update_status, assign_status))
            # FIXME: Remove this custom error message.
            # Must be replaced by the STB assign screen flow
            ErrorPage(
                title=_("STARTUP TIMEOUT ERROR"),
                description=_("Please restart."),
                error_message=status_message).show()
            self.hide_splashscreen()

    def loop_check_document_parsing(self):
        """Document parsing Node.

        Check if all the broadcasted documents required to correctly
        run the application have been parsed.
        """
        log.info("[BOOT] loop_check_document_parsing")
        brocker_document_dict = OCIDService().broker_document_state_dict
        # Check that each broadcasted document is parsed
        for document, state in brocker_document_dict.iteritems():
            if document is "MESSAGES":
                log.info("[BOOT] bypassing MESSAGES.xml parsing dependency")
                continue

            if state != PARSED:
                log.warning("Document %r not parsed. "
                            "Waiting for the parsing to complete", document)
                # Retry: when the document will be parsed
                OCIDService().register(self.loop_check_document_parsing,
                                       document_parsing=document)
                break
        else:
            # Try unregister the callback from the document_parsing signal
            try:
                OCIDService().unregister(self.loop_check_document_parsing)
            except ValueError:
                log.debug("Callback not registered to document_parsing signal")

            # All documents are parsed, Open startup pages
            # Check that the showcases for the default user have been
            # retrieved
            def _show_case_error_page():
                log.error("Showcase not available, please reboot !")
                ErrorPage(title=_("STARTUP TIMEOUT ERROR"),
                          description=_("Please restart."),
                          error_message="Show cases not available").show()
                self.hide_splashscreen()

            showcase_status = BTAPIService().get_showcase_status()
            log.info("get_showcase_status: %s", showcase_status)

            if showcase_status == SHOW_CASE_OK.uid:
                # Configure the UI and show the first pages
                Task(self.finish).start()
            elif showcase_status == SHOW_CASE_ON_GOING.uid:
                # Wait while OCI is retrieving the showcases to continue
                log.warning("Showcase status not available."
                            "Waiting for the fetching to end.")

                def _show_ready_cb(show_case_ready):
                    log.info("showcase ready: %s", show_case_ready)
                    if not show_case_ready:
                        _show_case_error_page()
                    else:
                        try:
                            BTAPIService().unregister(_show_ready_cb)
                        except ValueError:
                            log.debug("Callback not registered to "
                                      " showcase_ready signal.")
                        Task(self.finish).start()

                BTAPIService().register(_show_ready_cb,
                                        showcase_ready="no_option")
            else:
                _show_case_error_page()

    def _check_player_status(self):
        """Documents are already parsed at this stage.

        Then all we have to do is to send AVR logs, one after another
        as confirmed by xpaquet and alex.
        NB: because we don't know the different boot paths.
        """
        while True:
            if TVPlayer().stream_status == 'OK':
                AVR().send("STBSTATUS-BASICDTV")
                AVR().send("STBSTATUS-FULLSERVICE")
                raise StopIteration()
            else:
                yield

    def _init_iml(self):
        # The function is defined only so it can be mocked in unit tests
        # Bind IML IBS pages
        from imlm.ibs import IBS
        from adele_app.pages.iml.ibs_waiting_page import IBSWaitingPage
        from adele_app.pages.iml.ibs_pincode_page import IBSPincodePage
        from adele_app.pages.iml.ibs_contact_selection_page import \
            IBSContactSelectionPage
        from adele_app.pages.iml.ibs_display_user_page import \
            IBSDisplayUserPage
        IBS().set_pages(pincode_page=IBSPincodePage,
                        contact_selection_page=IBSContactSelectionPage,
                        display_user_page=IBSDisplayUserPage,
                        waiting_page=IBSWaitingPage)

    def finish(self):
        """Finish Node.

        Initialize the part of the application that are dependent to
        the middleware.
        """
        log.info("[BOOT] finish")
        save_stb_name()

        # Register the UI interface on dbus
        try:
            from leon_mal.services.dial import DIALNetworkObserver
            DIALNetworkObserver().start()
            from wydbus import WyDbus
            WyDbus().request_name("com.wyplay.ui")
            log.info("UI registered on dbus on com.wyplay.ui")
        except Exception, e:
            log.warning("Can't wait for UI dbus interface : %r", e)

        # Try unregister the callback from the showcase_ready
        # signal
        try:
            BTAPIService().unregister(self.finish)
        except ValueError:
            log.debug("Callback not registered to showcase_ready signal.")

        if self.save_pbu_lang:
            set_config_language(self.save_pbu_lang, check='boot')
            self.save_pbu_lang = None
        else:
            lang = get_config_language()
            if lang != get_locale_language():
                set_locale_language(lang)

            # Workaround for defect #88563
            # We are not sure why the audio language would not be the UI language,
            # but it seems that it happens...
            # This is a workaround to force mediarenderer to use the UI language.

            def _set_audio_language_cb(request):
                if request.is_failed():
                    log.error("Set Audio Language Failed with error %s",
                            request.return_code)
                else:
                    log.info("Set Audio Language Succeeded")

            # Update the default audio language
            audio_config = MediaRoot().get_service('audio_config')
            audio_config.set_preferred_languages(
                        priorities=[lang.iso2],
                        callback=_set_audio_language_cb)

        if self.is_migration and self.first_page != "pin_code_change":
            # Software was migrated from Charles so we need to play
            # the setup wizard flow.
            self.first_page = 'pin_code_change'

        # Here we know that subscriber_info is ready on OCI
        # This will initialize the value of the _opt_status attribute.
        OCIDService().init_opt_status()

        self._init_iml()

        if os.getenv('TV', 'true').lower() not in ('false', '0', 'no'):
            Task(self._check_player_status()).start(0.5, loop=True)

        # Set poster servers now and on global install updates.
        set_poster_servers()
        OCIDService().register(set_poster_servers,
                               document_parsing="GLOBALINSTALL")

        # Launch parental control manager singleton
        ParentalControl()

        # BOTTOM stack :
        # Shows the root page
        from adele_app.pages.root_page import RootPage
        RootPage().show()

        # TOP stack :
        # Shows the clock
        from adele_app.pages.common.clock import Clock
        clock = Clock()
        clock.show()  # keep splash screen visible
        wuk.application.move_on_top(wuk.application.get('BootPage'))

        # Shows the Pip, Keep clock visible
        from adele_app.pages.tv.top_page_pip import Pip
        Pip().show(keep_previous_visible=True, above=clock)
        os.system('pkill -HUP netflix')
        # Connect to a signal that is called when the board is not
        # assigned. Run this code once
        if not self._is_signal_connected:
            self._is_signal_connected = True

            def on_unassign(status):
                if status in STB_IS_NOT_ASSIGNED_TRIGGERS:

                    def goto_firstinstall():
                        # Stop the playback
                        BasePlayer().stop()

                        # Destroy all pages
                        for stack_name in wuk.application.stacks:
                            wuk.application.get_stack(stack_name).empty()

                        # Show the 1st install page
                        self.first_page = 'main_hub'
                        from adele_app.pages.first_install.neutral_language \
                            import NeutralLanguage
                        NeutralLanguage().show()

                        if status == get_error_code("TM_INACTIVE_ACCOUNT"):
                            # go to error cluster page...and be blocked until
                            # account is activated again (need a reboot)
                            from peewee.request import Request
                            req = Request()
                            req.start()
                            req.fail(status)
                            ErrorProxyService().caught(req)

                    # close Netflix application if needed
                    from adele_app.pages.netflix.netflix_page \
                        import NetflixPage, kill_netflix
                    if isinstance(wuk.application.get_stack('main')[-1],
                                  NetflixPage):
                        kill_netflix(callback=goto_firstinstall,
                                     netflix_zap_on_exit=False)
                    else:
                        goto_firstinstall()

            OCIDService().register(on_unassign, boot_status_update='no_option')
