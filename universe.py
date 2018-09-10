# -*- coding: utf-8 -*-
""" utility to manage the breadcrumb throughout the application
"""
import wuk
from peewee.debug import GET_LOGGER
from peewee.messages import emit
from wuk.pages import Page

from adele_app.custom_widget.clock.clock import (
    BREADCRUMB_SENDER, UPDATE_SERVICE_SIGNAL,
    HIDE_CLOCK_SIGNAL, SHOW_CLOCK_SIGNAL,
    SHOW_SERVICE_PROFILE_SIGNAL, HIDE_SERVICE_PROFILE_SIGNAL,
    BLINK_PROFILE_NAME_SIGNAL, STOP_BLINK_PROFILE_NAME_SIGNAL
)

log = GET_LOGGER(__name__)

_last_service_name = None


class UniverseMixin(object):

    display_bc = True
    animate_bc = True

    #: This attribute defines the name of the universe to display when
    #: the page is on top of the stack. If ``None`` (default), it will
    #: automatically be set with the name of the previous page in the
    #: stack.
    service_name = None

    def show(self, *args, **kw):
        # Forces service name definition: if no service name defined
        # in the page, uses the one of the previous page.
        if self.service_name is None:
            previous_page = kw.get("above", None)
            # If 'above' parameter was not specified during 'show',
            # previous page is top page of the stack.
            if not previous_page:
                try:
                    previous_page = wuk.application.get_stack(name=self.stack)[-1]
                except IndexError:
                    # This cas should almost never happen.
                    # Known case: PIN code after first install.
                    log.warning("Page has no service name set and no "
                                "previous page in stack.")
                    previous_page = None
            # If the name of the page was provided instead of the
            # instance, get the instance.
            elif not isinstance(previous_page, Page):
                previous_page = wuk.application.get(previous_page,
                                                stack=self.stack)
            try:
                self.service_name = previous_page.service_name
            except AttributeError:
                # These pages shall be fixed!
                log.error("Page %s does not have a 'service_name' attribute "
                          "(fix this ASAP!).", previous_page)

        super(UniverseMixin, self).show(*args, **kw)

        # Update breadcrumb.
        if wuk.application.is_on_top(self):
            self.update_universe()

    def on_stack_move(self, *args, **kwargs):
        if wuk.application.is_on_top(self):
            self.update_universe()
        super(UniverseMixin, self).on_stack_move(*args, **kwargs)

    def update_universe(self):
        log.info('Page %r on top: display_bc=%r, animate_bc=%r, '
                 'service_name=%r',
                 self, self.display_bc, self.animate_bc, self.service_name)

        # Changes for prod-debug
        rootPage = wuk.application.get("RootPage")
        k = self.__class__
        rootPage.set_page_name("%s" % k.__name__)

        if self.display_bc:
            show_bc(self.animate_bc)
        else:
            hide_bc(self.animate_bc)

        if self.service_name is not None:
            set_service_name(self.service_name)

    def refresh_page(self, callback=None):
        """to be used to refresh some elements of a page, typically after a back to said page"""
        if callback is not None:
            callback()
        return True

def show_bc(anim=False):
    """ Show the breadcrumb.
    """
    emit(SHOW_CLOCK_SIGNAL, BREADCRUMB_SENDER, animate=anim)
    emit(SHOW_SERVICE_PROFILE_SIGNAL, BREADCRUMB_SENDER)


def hide_bc(anim=False):
    """ Hide the breadcrumb.
    """
    emit(HIDE_CLOCK_SIGNAL, BREADCRUMB_SENDER, animate=anim)
    emit(HIDE_SERVICE_PROFILE_SIGNAL, BREADCRUMB_SENDER)


def show_wifi_logo():
    emit("show_wifi_logo", BREADCRUMB_SENDER)


def hide_wifi_logo():
    emit("hide_wifi_logo", BREADCRUMB_SENDER)


def set_service_name(name):
    """ Set the service name with the given name """
    emit(UPDATE_SERVICE_SIGNAL, BREADCRUMB_SENDER, name=name)


def start_blinking():
    """ Profile name starts blinking
    """
    emit(BLINK_PROFILE_NAME_SIGNAL, BREADCRUMB_SENDER)


def stop_blinking():
    """ Profile name stops blinking
    """
    emit(STOP_BLINK_PROFILE_NAME_SIGNAL, BREADCRUMB_SENDER)
