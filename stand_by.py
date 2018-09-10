# -*- coding: utf-8 -*-
import os

from peewee.debug import GET_LOGGER
from peewee.notifier import mainthread, Task
from peewee.request import Request

from mal import MediaRoot
from mal.power_management import AWAKE, REBOOT

import wuk
from wuk.pages.stack_utils import PageNotFoundError

from leon_mal.avr import avr_send_start_tv
from leon_mal.platform import features, platform
from leon_mal.player import BasePlayer, TVPlayer
from leon_mal.restriction_management import ParentalControl
from leon_mal.services.wystandby_leon import WystandbyLeonService
from leon_mal.userconfigmgmt import UserConfigMgmt

from leon_app_tools.return_codes import (
    TRY_AGAIN, STB_ASSIGNED,
    SW_UPDATE_NOT_READY, NO_SW_UPDATE,
    SW_UPDATE_DOWNLOAD, SW_UPDATE_DOWNLOAD_IN_PROGRESS,
    SW_UPDATE_DOWNLOAD_FAILED, OCI_NETWORK_TIMEOUT,
    SW_UPDATE_NAND_WRITE, SW_UPDATE_NAND_WRITE_FAILED)


from leon_app_tools.fpdisplay_tools import FrontPanelTools

from adele_app.facilities import SimpleRequestJoiner, show_pip
from adele_app.facilities.main_hub import back_to_home
from adele_app.facilities.boot_fixtures import \
    refresh_front_pannel_on_play_failed
from adele_app.facilities.notifications import notifications_handler

import ConfigParser
from leon_mal.avr import AVR
from leon_mal.services.ocid import OCIDService
from adele_app.tools.utilities import hdd_info_parse
from adele_app.tools.utilities import avr_software_firmware_info_args

log = GET_LOGGER(__name__)
features_cec = "cec" in features


def _get_top_pages():
    # Get pages on top in main and popup stack before back to MainHub
    # To check that all graphicals objects for theses pages
    # are released before go to standby
    # Exclude top stack, (like clock , pip ...)  which pages
    # have 'keep_previous_visible' attr and "ready" stay in view state
    pages = wuk.application.get_stack("main") + \
        wuk.application.get_stack("popup")
    top_pages = []
    # Rebuild a list with all pages on top, except for
    # MainHub and pages with attr "keep_cached"
    # Cached pages released later in _releases_surfaces
    for page in pages[1:]:
        log.debug("%s view_states: %s", page, page.view_states)
        if "keep_cached" in page.view_states:
            continue
        top_pages.append(page)

    log.debug("STANDBY - pages to check: %s", top_pages)
    return top_pages


class Standby(object):

    def __init__(self):
        self.wystandby = WystandbyLeonService()
        self.user_config = UserConfigMgmt()
        self.front_panel = FrontPanelTools()
        self.player = TVPlayer()
        self.power_management = MediaRoot().get_service('power_management')

    def setup(self):
        self.wystandby.register(self._apd_warning_handler,
                                apd_warning="no_option")

        self.power_management.set_max_state(AWAKE)
        self.power_management.register(self._enter_standby, state='standby')
        self.power_management.register(self._leave_standby, state='awake')

        conf = self.user_config
        self.wystandby.enable_passive_standby(conf["coc_v8_enabled"])
        self.wystandby.set_apd_warning_timeout(conf["apd_warning_timeout"])
        self.wystandby.set_apd_timeout(conf["apd_timeout"])

    @mainthread
    def _apd_warning_handler(self):
        from adele_app.pages.notifications import StandByNotification
        StandByNotification.notify()

    def _enter_standby(self, changes):
        log.info("Entering standby")
        self.power_management = changes['state']
        wuk.application.hold = True
        record_page = None
        try:
            record_page = wuk.application.get_stack("main").get("BrowserPage")
        except:
            pass

        be_patient_page = None
        try:
            be_patient_page = wuk.application.get_stack("top").get("BePatientPage")
        except:
            pass
        # Check pages in main and popup stack
        top_pages = _get_top_pages()
        log.info("top pages: %s", top_pages)
        # Unregister signals
        self._toggle_signals_registration(register=False)
        # Differ this processing to speedup close_renderer_connection
        notifications_handler.close_all_notifications()
        # Disable front panel display
        log.info("standby front_panel")
        self.front_panel.in_standby(True)
        self.front_panel.reset_front_panel(power_on_icon=False)

        def _enter_standby_cb(requests):
            if all((r.is_succeeded() for r in requests)):
                log.info("Back to main hub with closed renderer connection.")
            else:
                log.error("Something went wrong while trying to return to "
                          "main and close renderer connection.")
            Task(self._check_top_pages_state(top_pages ,record_page, be_patient_page)).start()

        def _connection_close_cb():
            log.debug("_connection_close_cb")
            requests = SimpleRequestJoiner(_enter_standby_cb)
            requests.append(
                self._close_renderer_connection(callback=requests.callback))
            requests.append(
                self._return_to_main_hub(callback=requests.callback))

        save_last_pos_flag = False
        try:
            save_last_pos_flag = TVPlayer().is_playing_cutv or wuk.application.get("MediaPlayerPage")
        except PageNotFoundError:
            log.debug("MediaPlayerPage in not Found")

        if save_last_pos_flag:
            try:
                main_hub = wuk.application.get("MainHub")
                if main_hub.swimlane_enabled:
                    main_hub.nav_direction = 0
                if not TVPlayer().is_playing_cutv or main_hub.last_played_item == None:
                    main_hub.last_played_item = BasePlayer().playing_item
                BasePlayer()._set_playing_item_last_position(connecion_close_cb=_connection_close_cb)
            except:
                log.info("Got exception in _set_playing_item_last_position")
                _connection_close_cb()
        else:
            _connection_close_cb()

    def _return_to_main_hub(self, callback=None, args=None):
        """Return to main hub and deactivate auto-switch."""
        log.info("Returning to main hub...")
        request = Request(callback=callback, args=args)
        request.start()

        # for sending avr on standby
        self.send_avr_on_standby_task = Task(self.send_avr_on_standby)
        self.send_avr_on_standby_task.start(real_time=True,
                                        consider_idle=True,
                                        auto_clean=True)
        def _cb(req):
            if req.is_failed():
                log.error("back to home failed: %r", req.return_code)
                request.fail(return_code=req.return_code)
            else:
                request.succeed()
            main_hub = wuk.application.get("MainHub")
            if main_hub:
                main_hub.deactivate_auto_switch()
            request.call_final_callback()

        back_to_home(callback=_cb, args="StandBy")
        return request

    def _close_renderer_connection(self, callback=None, args=None):
        log.info("Closing renderer connection...")
        request = Request(callback=callback, args=args)
        request.start()

        # Parental Control: we relock the stream, if needed, and close the
        # rendered connection once it's done. This way, the UI will already
        # be locked when we leave standby. We don't need to check that the
        # relock worked correctly, we will relock anyway.
        renderer_connection = self.player.get_renderer_connection()
        if renderer_connection:
            log.debug("STANDBY - Relock stream")

            def _relock_stream_cb(req):
                if req.is_failed():
                    # Just log error, but go to standby anyway.
                    log.error("Relock stream failed: %r", req.return_code)
                else:
                    log.info("Relock stream done.")
                ParentalControl().set_lock_status("assets", True)
                ParentalControl().unregister_global_signals()
                if self.player.get_renderer_connection():
                    def _close_cb(req):
                        if req.is_failed():
                            # Just log error, but go to standby anyway
                            log.error("Close renderer connection failed: %r",
                                      req.return_code)
                            request.fail(return_code=req.return_code)
                        else:
                            log.info("Renderer connection closed.")
                            request.succeed()
                        request.call_final_callback()

                    log.debug("Close renderer connection.")
                    self.player.close_renderer_connection(callback=_close_cb)
                else:
                    request.succeed()
                    request.call_final_callback()

            renderer_connection.relock_stream(callback=_relock_stream_cb)
        else:
            log.debug("Renderer connection already closed.")
            request.succeed()
            request.call_final_callback_tasked()

        return request

    def _check_top_pages_state(self, top_pages, record_page, be_patient_page):
        """Make sure all pages above main hub are cleared."""
        log.info("Checking state of pages: %r", top_pages)
        while True:
            for page in list(top_pages):
                log.debug("%r view state: %r", page, page.view_states)
                if "ready" not in page.view_states:
                    top_pages.remove(page)
                    log.debug("Page %r is cleared.", page)
                else:
                    log.info("Page %r is not yet cleared.", page)
            if not top_pages:
                if record_page is not None:
                    log.info("BrowserPage : %s releasing it ", record_page)
                    record_page._unregister_nexus_released()
                else:
                    log.info("BrowserPage : not found ")

                if be_patient_page is not None:
                    log.debug("be_patient_page is cleared.")
                    be_patient_page.remove()
                else:
                    log.info("be_patient_page : not found ")

                log.info("All top pages are cleared.")
                root_page = wuk.application.get_stack("background")[0]

                main_hub = wuk.application.get('MainHub')
                main_hub.hide_main_hub_page_elements()

                root_page.enter_standby()
                log.info("Ready to go into standby!")
                self.power_management.ack_state_change()
                raise StopIteration
            else:
                log.debug("Pages not yet all cleared.")
                yield 0.05

    def _leave_standby(self, changes):
        log.info("Leaving standby")
        if features_cec:
            # CR CEC: re-activate cec,
            # if CEC not activated due to a user reboot (remote or sw update)
            # to be sure, cec will wake-up tv
            MediaRoot().get_service("cec").synchronize()
        # Enable front panel display
        self.front_panel.in_standby(False)
        self.front_panel.restore_front_panel()
        # As for now, no page is handling the Front Panel, so there is no page
        # to write on the front panel We manualy reset the text to "Welcome +"
        # for now, until some other components like the TVPlayer page handle
        # the Front Panel
        self.front_panel.display_text("Welcome +")

        try:
            # get the netflix page. If it fails, it throws a PageNotFoundError
            wuk.application.get('NetflixPage')
        except PageNotFoundError:
            # start renderer connexion if no netflix page
            # 'self.player' here is the TVPlayer, so calling start_renderer will
            # also call set_polling_mode on the TVPlayer, and reconnect it to
            # mediarenderer 'program changes' signals
            self.player.start_renderer(self._leave_standby_callback)

    def _leave_standby_callback(self, request):
        ParentalControl().register_global_signals()
        main_hub = wuk.application.get('MainHub')
        main_hub.zap_conflict['out_from_standby'] = True
        if main_hub.swimlane_enabled:
            main_hub.in_standby = False
            main_hub.last_swimlane_played_item = None
            main_hub.call_set_src_on_stack_move = True
            main_hub.show_main_hub_page_elements()
            main_hub.set_focused_name(item_list_name="swimlanes")

            def _standby_leave_callback():
                wuk.application.hold = False

            main_hub.update_access_point_and_swimlane(callback=_standby_leave_callback)
        root_page = wuk.application.get_stack("background")[0]
        root_page.leave_standby()

        self._toggle_signals_registration(register=True)
        self.power_management.ack_state_change()
        log.info("Successfully came out of standby!")

        # this callback refreshed the front pannel in case of play last_tunned
        # channel failure + throws the AVR log
        def on_play_cb(request):
            refresh_front_pannel_on_play_failed(request)
            if request.is_succeeded():
                #show operator messages
                main_hub = wuk.application.get("MainHub")
                main_hub.show_operator_message()
                #send AVR
                avr_send_start_tv(request)

        # redo hide_pip action on standby mode exit because it failed on
        # standby mode activation. We don't care for main hub status because
        # we are supposed to restore main hub on default status.
        show_pip(False)
        try:
            # get the netflix page. If it fails, it throws a PageNotFoundError
            wuk.application.get('NetflixPage')
        except PageNotFoundError:
            # Some check up are necessary for dortor repair.
            # They are sensibly the same than when the ethernet cable is inplug
            # So ask the RootPage to do a checkup
            wuk.application.get('RootPage').force_check_up(on_play_cb)

    def disable_standby(self, callback=None):
        """ When called, this tells the power manager that the max state is
        :data:`AWAKE` instead of :data:`REBOOT`.

        self.power_management.set_max_state(AWAKE)

        :param callback: callback to call when operation is done.
        :type callback: None or callable
        """
        self.power_management.set_max_state(AWAKE, callback=callback)

    def enable_standby(self, callback=None):
        """ When called, this tells the power manager that the max state is
        :data:`REBOOT` instead of :data:`AWAKE`.

        self.power_management.set_max_state(REBOOT)

        :param callback: callback to call when operation is done.
        :type callback: None or callable
        """
        self.power_management.set_max_state(REBOOT, callback=callback)

    def _toggle_signals_registration(self, register):
        hub = wuk.application.get('MainHub')

        if register:
            log.debug("STANDBY: force activate_auto_switch")
            hub.activate_auto_switch()
            # When going back from standby we always return to main hub so we
            # do not need here to do anything about the tv page
            if hub._tv_action is not None:
                hub._tv_action.connect_signals()
            else:
                log.error("TvWidgetAction is None")
            if self.player.get_renderer_connection() is not None:
                self.player.stop_polling_position()
                self.player.register_operation_denied(hub._on_operation_denied)
                self.player.register_other_operation_denied(
                    hub._other_operation_denied_handler)
        else:
            try:
                tv_page = wuk.application.get('TVPage')
                tv_page.disconnect_signals()
            except PageNotFoundError:
                tv_page = None
                log.debug("TVPage not found for standby, no need to "
                          "disconnect/connect signals")

            if hub._tv_action is not None:
                hub._tv_action.disconnect_signals()
            else:
                log.error("TvWidgetAction is None")

            if self.player.get_renderer_connection() is not None:
                self.player.unregister(hub._on_operation_denied)
                self.player.unregister(hub._other_operation_denied_handler)
                self.player.stop_polling_position()

    def set_ui_ready(self):
        """Set that the UI is now ready to enter standby at anytime"""
        # Write an empty file to notify wycrs we are ready to receive signals
        # this variable is necessary to active standby ,
        # added to wycrs beacause Charles Ui dosen't manage the set_max_state
        # if you want to remove this, you have to add
        # a use flag in Wycrs to enable/disable UI_STANDBY_READY
        try:
            open(os.environ["UI_STANDBY_READY"], 'w+').close()
            log.info("UI_standby_enabled.")
        except:
            log.error("Cannot set standby enabled (missing UI_STANDBY_READY "
                      "in environment?)")

        # Allow android system to go into standby when the UI is ready

    def send_avr_on_standby(self):
        try:
            config = ConfigParser.ConfigParser()
            config.read("/etc/params/stores/wystandby.cfg")
            last_sleep_state = config.get("leon", "state")      #Exception in this line sends no AVR
        except:
            last_sleep_state = 0

        power_cycle_avr_flag = False
        update_status = OCIDService().sw_update_state_get()

        if update_status == SW_UPDATE_NAND_WRITE:
            power_cycle_avr_flag = True
            if last_sleep_state == 1:
                power_cycle_header_list = ["TSU", "STBY", "STBY","NULL"]
            else:
                power_cycle_header_list = ["TSU", "POWEROFF", "STBY","NULL"]
        else:
            if last_sleep_state == 1:
                power_cycle_avr_flag = True
                power_cycle_header_list = ["STBY", "STBY", "STBY","NULL"]

        sw_fw_element_list = ['NULL']*12
        hdd_info_list = ['NULL']*19

        if power_cycle_avr_flag == True:
            #For NOR-NAND elements
            nor_nand_info_list_null = ["NULL"]*12
            if platform in ['v5', 'v4']:
                hdd_info_list = hdd_info_parse()
            if platform == "v5":
                sw_fw_element_list = avr_software_firmware_info_args()

            sw_fw_element_list.extend(nor_nand_info_list_null)
            avr_tuple = tuple(power_cycle_header_list + sw_fw_element_list + hdd_info_list)
            log.info("Sending TSU/Normal boot AVR")
            AVR().send("POWER-CYCLE", avr_tuple)

def is_passive_standby_enabled():
    """
    Returns whether the passive standby is currently enabled in settings
    (Aka 'coc v8' on v4/v5, and 'eco' mode on v6).

    :rtype: ``bool``
    """
    ui_config_value = UserConfigMgmt()["coc_v8_enabled"]
    if platform == 'v6':
        # get the current user choice through the system property
        # deep standby mode is S5 power state mode, normal is S2
        SystemProperties = autoclass('android.os.SystemProperties')
        standby_mode_system_property = SystemProperties.get(
            'persist.sys.power.offstate', 'S2')
        android_config_value = standby_mode_system_property == 'S5'
        if android_config_value != ui_config_value:
            # The value of the android property may have been updated by
            # the standby popup for example, in java side
            log.error("Bad passive standby conf: configstore value is %s "
                      "whereas android value is %s",
                      ui_config_value, android_config_value)
            # TODO: update the value stored in configstore ?
        # let's take the value of the android property anyway
        return android_config_value
    else:
        return ui_config_value