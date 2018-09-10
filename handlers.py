# -*- coding: utf-8 -*-
from mal import MediaRoot
from peewee.debug import GET_LOGGER
from peewee.browse_criteria import Meta, And
import wuk
from wuk.pages import key_handlers
from wuk.pages.stack_utils import PageNotFoundError

from com.leon.avr import AVR

from leon_mal.player import BasePlayer
from leon_mal.platform import get_platform
from leon_mal.configmgmt import ConfigMgmt
from leon_mal.userconfigmgmt import UserConfigMgmt

from adele_app.background import scroll_bg_lines
from adele_app.facilities.cutv import CUTV_INACTIVITY, cutv_inactivity_task
from adele_app.facilities.cutv import cutv_exit_notification
from adele_app.facilities.main_hub import back_to_home
from adele_app.facilities.channel_list_proxy import retrieve_all_radio_search_criteria

import time


log = GET_LOGGER(__name__)

v4_platform = get_platform() == "v4"


def show_helparea(helparea):
    from adele_app.facilities.iml_utils import get_webaccess, launch_iml

    def _help_entry(request):
        if request.is_succeeded():
            svc = request.data["result"]
            launch_iml(svc, html_meta_name="target_html",
                       iml_meta_name="target",
                       parameters={'helparea': helparea})
        else:
            log.error("Cannot get help element from webaccess")
    get_webaccess("HELP", callback=_help_entry)

def get_channel_search_criteria(channel_id=None):
    """Get channel search criteria.

    Returns search criteria according to account subscription for
    all channels or only one channel if 'channel_id' is specified.
    """
    search_criteria = [
        Meta("valid_subscription") == True,
    ]

    if channel_id:
        search_criteria.append(Meta("id") == channel_id)
    profile_hd = ConfigMgmt().get_hd_interest() and ConfigMgmt().get_hd_reception()

    if not profile_hd:
        search_criteria.append(Meta("resolution") == "SD")

    return And(*search_criteria)

class BaseEventHandler(key_handlers.BaseEventHandler):

    """Base Event Handler used in all pages of the application."""

    _power_management = None

    def __init__(self, page):
        self._power_management = MediaRoot().get_service('power_management')
        key_handlers.BaseEventHandler.__init__(self, page)

    if v4_platform:
        def __call__(self, event):
            log.info("[event/handler] %s / %s", event, self)
            self._blink(event)
            cutv_inactivity_task.start(CUTV_INACTIVITY)
            if "ready" not in self.page.view_states:
                log.info("Page not ready, dropping key.")
                return True
            return super(BaseEventHandler, self).__call__(event)

        def _blink(self, event):
            if not (str(event).lower()).startswith("short"):
                # Front Panel: set Power Led blink
                FrontPanelTools().set_power_led_blink()
    else:
        def __call__(self, event):
            log.info("[event/handler] %s / %s", event, self)
            cutv_inactivity_task.start(CUTV_INACTIVITY)
            if "ready" not in self.page.view_states:
                log.info("Page not ready, dropping key.")
                return True
            return super(BaseEventHandler, self).__call__(event)

    def event_back(self, event):
        self.page.hide()
        return True

    def event_home(self, event, check_cutv=True):
        """ Go to Screen #001.2 - Hub (GIS 000 - General Nav).

        If check_cutv is enabled, a popup will appear to warn the
        user that he is going to leave the CUTV playback when it
        is the case.
        """
        AVR().send("HOTKEY", "menu")

        # JIRA 1621 : CR4507-US10 Touchpoint banners: Cascading Message Display
        # Below two lines added for MainHUb to display Operator Message 
        # when HOTKEY is pressed.
        main_hub = wuk.application.get('MainHub')
        main_hub.show_operator_messages = True
        main_hub.message_on_top = False
        
        def _back_to(request):
            if not request.is_succeeded():
                log.error("can't close context when going back to main hub from menu key")
                return

            page = request.args[0] if main_hub.swimlane_enabled else request.args
            if hasattr(page, 'close_context'):
                # If we are in a context, we close it before.
                page.close_context()

        if main_hub.swimlane_enabled:
            back_to_home(args=(self.page, "swimlane"))
        else:
            back_to_home(args=self.page)
        return True

    def event_help(self, event):
        """ Manages the context sensitive help system """
        try:
            helparea = self.page.helparea
        except AttributeError:
            helparea = "hlpMenu.efx"
        log.info("Help requested: showing %r", helparea)
        show_helparea(helparea)
        return True

    def _show_filter_layer(self, filter_layer_class):
        """Show filter layer.

        Show filter layer above main hub if not already in stack.
        then, go back to filter layer to have it on top of stack.
        """
        filter_layer_class_name = filter_layer_class.__name__
        if filter_layer_class_name not in wuk.application:
            page = filter_layer_class()
            page.show(above="MainHub")
        wuk.application.back_to(filter_layer_class_name)

    @cutv_exit_notification
    def event_dtv(self, event, indirect=False):
        """event handler for the Digital TV button,
        displays fullscreen Live TV"""

        if not indirect:
            AVR().send("HOTKEY", "digitaltv")

        from adele_app.pages.tv.tv_page import TVPage
        from leon_mal.player import TVPlayer
        from leon_mal.radio import is_radio_channel
        from adele_app.pages.player.media_player_page import MediaPlayerPage

        # Jira Bug STBV4V5-1176
        from leon_mal.userconfigmgmt import UserConfigMgmt
        from adele_app.pages.player.demo_page import DemoVideoPage
        from adele_app.pages.netflix.netflix_page import NetflixPage
        # Jira Bug STBV4V5-1563,1568
        from adele_app.pages.iml.html import ImlBrowserPage

        try:
            top_page = wuk.application.get_stack("main")[-1]
            # Jira Bug STBV4V5-1563,1568
            if not isinstance(top_page,(MediaPlayerPage , ImlBrowserPage, TVPage)):
                if is_radio_channel(TVPlayer().current_channel):
                    return self.event_radio(event)
                else:
                    log.info("Current Channel is not Radio Channel")
        except PageNotFoundError:
            top_page = False
            log.info("Top page not found in the stack")

        # Jira Bug STBV4V5-1176
        show_banner = True
        if isinstance(top_page, (NetflixPage, MediaPlayerPage, DemoVideoPage)) \
            and UserConfigMgmt()["channel_change_display_info"]:
            show_banner = False
        if not is_radio_channel(TVPlayer().current_channel):
            def _cb_to_live(request):
                if request.is_failed():
                    log.error("Could not go go back to Live")
                else:
                    log.info("Could go back to live")
                tv_page.back_to_tv()

            main_hub = wuk.application.get('MainHub')
            tv_page_on_top = False
            # Get/create TVPage
            try:
                tv_page = wuk.application.get('TVPage')
                tv_page_on_top = True
                log.info("TV Page found in the stack")
                # Application.move_on_top(tv_page)   #should be included with data layer removal v2, but TV trick player has graphical bugs with this
            except PageNotFoundError:
                tv_page = TVPage()
                log.info("TV Page not found in the stack")
                tv_page.show(above=main_hub, ignore_banner=show_banner)
                tv_page.back_to_tv()
            # Focus television on the main hub, for when we go back from fullscreen TV
            if tv_page_on_top:
                if tv_page.is_playing_live:
                    tv_page.back_to_tv()
                else:
                    TVPlayer().play_live(callback=_cb_to_live)
                main_hub.focus_tv(instant_anim=True)

        else:
            def _play_channel_cb(request):
                request.caller.close()
                if self.page.requests is not None and request in self.page.requests:
                    self.page.requests.remove(request)
                if not request.is_succeeded():
                    log.error("Could not get tv channel: %r", request)
                else:
                    tv_channel = request.data['result'][0]

                    def _cb_to_live(request):
                        if request.is_failed():
                            log.error("Could not go go back to Live")
                        else:
                            log.info("Could go back to live")
                        tv_page.back_to_tv()

                    main_hub = wuk.application.get('MainHub')
                    tv_page_on_top = False
                    # Get/create TVPage
                    try:
                        tv_page = wuk.application.get('TVPage')
                        tv_page_on_top = True
                        log.info("TV Page found in the stack")
                        tv_page._info_page_shown = False
                        tv_page.zap_with_asset(channel=tv_channel)
                        # Application.move_on_top(tv_page)   #should be included with data layer removal v2, but TV trick player has graphical bugs with this
                    except PageNotFoundError:
                        tv_page = TVPage()
                        log.info("TV Page not found in the stack")

                        tv_page.show(above=main_hub, ignore_banner=show_banner, channel=tv_channel)
                        tv_page.back_to_tv()
                        tv_page.zap_with_asset(channel=tv_channel)
                    # Focus television on the main hub, for when we go back from fullscreen TV
                    if tv_page_on_top:
                        if tv_page.is_playing_live:
                            tv_page.back_to_tv()
                        else:
                            TVPlayer().play_live(callback=_cb_to_live)
                        main_hub.focus_tv(instant_anim=True)

            last_tuned_channel_id = UserConfigMgmt()["last_tuned_radio"]['channel']
            last_tuned_channel_criteria = get_channel_search_criteria(
                    channel_id=last_tuned_channel_id)
            datasource = MediaRoot().get_container("channel").browse(
                metadata="all",
                search_criteria=last_tuned_channel_criteria,
                order="+lcn",
                max_hits=1,
                static=True)
            datasource.get(count=1,
                           callback=_play_channel_cb)
        return True

    @cutv_exit_notification
    def event_radio(self, event):
        AVR().send("HOTKEY", "radio")

        from adele_app.pages.tv.tv_page import TVPage
        from leon_mal.player import TVPlayer
        from leon_mal.radio import is_radio_channel
        from adele_app.pages.player.media_player_page import MediaPlayerPage
        from adele_app.pages.iml.html import ImlBrowserPage

        # Jira Bug STBV4V5-1176
        from leon_mal.userconfigmgmt import UserConfigMgmt
        from adele_app.pages.player.demo_page import DemoVideoPage
        from adele_app.pages.netflix.netflix_page import NetflixPage

        try:
            top_page = wuk.application.get_stack("main")[-1]
            if not isinstance(top_page,(MediaPlayerPage , TVPage, ImlBrowserPage)):
                if not is_radio_channel(TVPlayer().current_channel):
                    return self.event_dtv(event)
                else:
                    log.info("Current Channel is Radio Channel")

        except PageNotFoundError:
            top_page = False
            log.info("Top page not found in the stack")

        # Jira Bug STBV4V5-1176
        from leon_mal.userconfigmgmt import UserConfigMgmt
        show_banner = True
        if isinstance(top_page, (NetflixPage, MediaPlayerPage, DemoVideoPage)) \
            and UserConfigMgmt()["channel_change_display_info"]:
            show_banner = False

        def _play_radio_cb(request):
            request.caller.close()
            if not request.is_succeeded():
                log.error("Could not get radio channel: %r", request)
            else:
                radio_channel = request.data['result'][0]

                def _cb_to_live(req):

                    if req.is_failed():
                        log.error("Could not go back to live")
                    else:
                        log.info("Could go back to live")
                    tv_page.back_to_tv()

                main_hub = wuk.application.get('MainHub')
                # Get / create TVPage
                try:
                    tv_page = wuk.application.get("TVPage")
                    log.info("TV Page found in the stack")
                    tv_page._info_page_shown = False
                    tv_page.zap_with_asset(channel=radio_channel)
                except PageNotFoundError:
                    tv_page = TVPage()
                    log.info("TV Page not found in the stack")
                    tv_page.show(above=main_hub,channel=radio_channel, ignore_banner=show_banner)
                    tv_page.back_to_tv()
                    tv_page.zap_with_asset(channel=radio_channel)
                # Focus television on the main hub, for when we go back from fullscreen TV
                if tv_page.is_playing_live:
                    tv_page.back_to_tv()
                else:
                    TVPlayer().play_live(callback=_cb_to_live)
                main_hub.focus_tv(instant_anim=True)

        last_tuned_radio_id = UserConfigMgmt()["last_tuned_radio"]['radio']
        search_criteria = retrieve_all_radio_search_criteria()

        if last_tuned_radio_id :
            search_criteria = And(Meta("id") == last_tuned_radio_id, search_criteria)

        datasource = MediaRoot().get_container("channel").browse(metadata="all", max_hits=1,
                                                                 order="+lcn",
                                                                 search_criteria=search_criteria)
        datasource.get(count=1, callback=_play_radio_cb)

        return True

    def event_on_demand(self, event):
        """event handler for on_demand tv  button,
        """
        AVR().send("HOTKEY", "onDemand")
        from leon_mal.services.ocid import OCIDService
        error_code = OCIDService().update_vod_parent(param=0)
        from adele_app.pages.store import StoreFilterLayer
        # Fix for #80370.
        self._show_filter_layer(StoreFilterLayer)
        return True

    def event_pvr(self, event):
        AVR().send("HOTKEY", "pvr")
        from adele_app.pages.library.library_filter_layer import \
            LibraryFilterLayerPage
        # Fix for #80097 and #83259 .
        self._show_filter_layer(LibraryFilterLayerPage)
        return True

    def event_tv_guide(self, event):
        """event handler for tv guide button,
        focus current program of current channel in EPG grid,
        even if in CUTV or TS"""
        AVR().send("HOTKEY", "tvguide")

        from adele_app.pages.grid.epg_grid import EpgGridPage
        from leon_mal.player import TVPlayer

        main_hub = wuk.application.get("MainHub")

        def _show_epg(timestamp):
            # get/create EpgGridPage
            try:
                epg = wuk.application.get("EpgGridPage")
                log.info("EPG Grid Page found in the stack")
            except PageNotFoundError:
                if timestamp == -1:
                    timestamp = time.time()
                epg = EpgGridPage(timestamp)
                log.info("EPG Grid Page not found in the stack")
                epg.show(above=main_hub)
                wuk.application.back_to(epg)
            else:
                # if EpgGridPage already exists, we re-instantiate
                # it then hide the first one
                epg = EpgGridPage(timestamp)
                epg.show(above=main_hub)
                wuk.application.back_to(epg)

            main_hub.focus_tv(instant_anim=True)

        def _cb(req):
            if req.is_failed():
                log.error("Could not get current program time stamp")
                timestamp = None
            else:
                log.info("Could get current program time stamp")
                timestamp = req.data["result"]["position"] / 1000

            _show_epg(timestamp=timestamp)

        if TVPlayer().is_timeshift or TVPlayer().is_playing_cutv:
            # specific case when cutv or timeshit, open EPG on current program
            BasePlayer().get_renderer_connection().get_positions(
                unit='ms', absolute=True, callback=_cb)
        else:
            # open EPG on live program
            _show_epg(timestamp=time.time())

        return True

    def event_pause(self, event):
        return True

    event_play = event_record = event_stop = event_wheel_fw = \
    event_wheel_rw = _ignore_event = event_playpause = event_pause


class FilterLayerEventHandler(BaseEventHandler):

    def event_ok(self, event):
        self.page.launch_action()
        return True

    def event_back(self, event):
        wuk.application.back_to("MainHub")
        return True

    def event_up(self, event):
        self._vertical_event(event,
                             self.page.previous_focusable_element,
                             self.page.focus_previous,
                             -1)
        return True

    def event_down(self, event):
        self._vertical_event(event,
                             self.page.next_focusable_element,
                             self.page.focus_next,
                             1)
        return True

    def _vertical_event(self, event, element, focus_action, shift):
        """Generic vertical scroll.

        element: previous|next_focusable_element
        focus_action: focus_previous|next method
        shift: -1/+1
        """
        if element is None:
            return
        if element.data_source:
            # All horizontal lists are centered.
            element.select(element.center_index,
                           absolute=True,
                           callback=self._vertical_event_cb,
                           args=(focus_action, shift),
                           wait_view=True,
                           instant_anim=True)

    def _vertical_event_cb(self, request):
        if request.return_code not in ("ok", "already_selected"):
            log.warning(request)
            return
        focus_action, shift = request.args
        focus_action()
        self.page.titles_list.select(shift)
        self.page.navigation_list.select(shift)
        scroll_bg_lines(self.page.focused_element)

    def event_left(self, event):
        self.page.focused_element.select(-1, callback=self._horizontal_cb)
        return True

    def event_right(self, event):
        self.page.focused_element.select(1, callback=self._horizontal_cb)
        return True

    def _horizontal_cb(self, request):
        if request.is_failed():
            return
        scroll_bg_lines(self.page.focused_element, request=request)
