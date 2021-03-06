# -*- coding: utf-8 -*-

###########################################################################
#    SeriesFinale
#    Copyright (C) 2009 Joaquim Rocha <jrocha@igalia.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
###########################################################################

import hildon
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import gettext
import locale
import pango
import os
import re
import time
from xml.sax import saxutils
from series import SeriesManager, Show, Episode
from lib import constants
from lib.connectionmanager import ConnectionManager
from lib.portrait import FremantleRotation
from lib.util import get_color
from settings import Settings
from asyncworker import AsyncWorker, AsyncItem
from enhancedtreeview import EnhancedTreeView

_ = gettext.gettext

gtk.gdk.threads_init()

class MainWindow(hildon.StackableWindow):

    def __init__(self):
        super(MainWindow, self).__init__()

        # i18n
        languages = []
        lc, encoding = locale.getdefaultlocale()
        if lc:
            languages = [lc]
        languages += constants.DEFAULT_LANGUAGES
        gettext.bindtextdomain(constants.SF_COMPACT_NAME,
                               constants.LOCALE_DIR)
        gettext.textdomain(constants.SF_COMPACT_NAME)
        language = gettext.translation(constants.SF_COMPACT_NAME,
                                       constants.LOCALE_DIR,
                                       languages = languages,
                                       fallback = True)
        _ = language.gettext

	# Autorotation
 	self._rotation_manager = FremantleRotation(constants.SF_COMPACT_NAME,
                                                  self)

        self.connection_manager = ConnectionManager()
        self.connection_manager.connect('connection-changed',
                                        self._on_connection_changed)

        self.series_manager = SeriesManager()
        self.settings = Settings()
        hildon.hildon_gtk_window_set_progress_indicator(self, True)
        save_pid = AsyncItem(self.save_current_pid,())
        load_conf_item = AsyncItem(self.settings.load,
                                   (constants.SF_CONF_FILE,))
        load_shows_item = AsyncItem(self.series_manager.load,
                                    (constants.SF_DB_FILE,),
                                    self._load_finished)
        self.series_manager.connect('show-list-changed',
                                    self._show_list_changed_cb)
        self.series_manager.connect('get-full-show-complete',
                                    self._get_show_complete_cb)
        self.series_manager.connect('update-show-episodes-complete',
                                    self._update_show_complete_cb)
        self.series_manager.connect('update-shows-call-complete',
                                    self._update_all_shows_complete_cb)
        self.series_manager.connect('updated-show-art',
                                    self._update_show_art)

        self.request = AsyncWorker(True)
        self.request.queue.put(save_pid)
        self.request.queue.put(load_conf_item)
        self.request.queue.put(load_shows_item)

        old_pid = self.get_previous_pid()

        if old_pid:
            show_information(self, _("Waiting for previous SeriesFinale to finish...."))
            gobject.timeout_add(2000,
                                self.run_after_pid_finish,
                                old_pid, self.request.start)
        else:
            self.request.start()

        self.shows_view = ShowsSelectView()
        self.shows_view.connect('row-activated', self._row_activated_cb)
        self.shows_view.connect_after('long-press', self._long_press_cb)
        self.set_title(constants.SF_NAME)
        self.set_app_menu(self._create_menu())
        self.live_search = LiveSearchEntry(self.shows_view.tree_model,
                                           self.shows_view.tree_filter,
                                           ShowListStore.SEARCH_COLUMN)
        area = hildon.PannableArea()
        area.add(self.shows_view)
        box = gtk.VBox()
        box.pack_start(area)
        box.pack_end(self.live_search, False, False)
        self.add(box)
        box.show_all()
        self.live_search.hide()

        self.connect('delete-event', self._exit_cb)
        self._update_delete_menu_visibility()

        self.connect('key-press-event', self._key_press_event_cb)

        self._have_deleted = False

    def save_current_pid(self):
        pidfile = open(constants.SF_PID_FILE, 'w')
        pidfile.write('%s' % os.getpid())
        pidfile.close()

    def get_previous_pid(self):
        if not os.path.isfile(constants.SF_PID_FILE):
            return None

        pidfile = open(constants.SF_PID_FILE, 'r')
        pid = pidfile.readline()
        pidfile.close()
        if os.path.exists('/proc/%s' % pid):
            return pid
        else:
            os.remove(constants.SF_PID_FILE)
            return None
    
    def run_after_pid_finish(self, pid, callback):
        if os.path.exists('/proc/%s' % pid):
            return True

        callback()
        return False

    def _load_finished(self, dummy_arg, error):
        self.shows_view.set_shows(self.series_manager.series_list)
        hildon.hildon_gtk_window_set_progress_indicator(self, False)
        self.request = None
        self._update_delete_menu_visibility()
        self.shows_view.sort()
        self.sort_by_name_filter.set_active(
                                 self.settings.getConf(Settings.SHOWS_SORT) != \
                                 Settings.RECENT_EPISODE)
        self._applyRotation()
        self.series_manager.auto_save(True)

    def _create_menu(self):
        menu = hildon.AppMenu()

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Add shows'))
        button.connect('clicked', self._add_shows_cb)
        menu.append(button)

        self.sort_by_ep_filter = hildon.GtkRadioButton(
                                        gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.sort_by_ep_filter.set_mode(False)
        self.sort_by_ep_filter.set_label(_('Sort by ep. date'))
        menu.add_filter(self.sort_by_ep_filter)
        self.sort_by_name_filter = hildon.GtkRadioButton(
                                          gtk.HILDON_SIZE_FINGER_HEIGHT,
                                          group = self.sort_by_ep_filter)
        self.sort_by_name_filter.set_mode(False)
        self.sort_by_name_filter.set_label(_('Sort by name'))
        menu.add_filter(self.sort_by_name_filter)
        self.sort_by_name_filter.set_active(
            self.settings.getConf(Settings.SHOWS_SORT) != \
                Settings.RECENT_EPISODE)
        self.sort_by_ep_filter.connect('clicked',
                                lambda w: self.shows_view.sort_by_recent_date())
        self.sort_by_name_filter.connect('clicked',
                             lambda w: self.shows_view.sort_by_name_ascending())

        self.delete_menu = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.delete_menu.set_label(_('Delete shows'))
        self.delete_menu.connect('clicked', self._delete_shows_cb)
        menu.append(self.delete_menu)

        self.update_all_menu = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.update_all_menu.set_label(_('Update all'))
        self.update_all_menu.connect('clicked', self._update_all_shows_cb)
        menu.append(self.update_all_menu)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Settings'))
        button.connect('clicked', self._settings_menu_clicked_cb)
        menu.append(button)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('About'))
        button.connect('clicked', self._about_menu_clicked_cb)
        menu.append(button)

        menu.show_all()
        return menu

    def _add_shows_cb(self, button):
        new_show_dialog = NewShowsDialog(self)
        response = new_show_dialog.run()
        new_show_dialog.destroy()
        if response == NewShowsDialog.ADD_AUTOMATICALLY_RESPONSE:
            self._launch_search_shows_dialog()
        elif response == NewShowsDialog.ADD_MANUALLY_RESPONSE:
            self._new_show_dialog()

    def _delete_shows_cb(self, button):
        delete_shows_view = ShowsDeleteView(self.series_manager)
        delete_shows_view.shows_select_view.set_shows(self.series_manager.series_list)
        delete_shows_view.show_all()
        self._have_deleted = True

    def _launch_search_shows_dialog(self):
        search_dialog = SearchShowsDialog(self, self.series_manager, self.settings)
        response = search_dialog.run()
        show = None
        if response == gtk.RESPONSE_ACCEPT:
            if search_dialog.chosen_show:
                hildon.hildon_gtk_window_set_progress_indicator(self, True)
                show_information(self,
                                 _('Gathering show information. Please wait...'))
                if search_dialog.chosen_lang:
                    self.series_manager.get_complete_show(search_dialog.chosen_show,
                                                          search_dialog.chosen_lang)
                else:
                    self.series_manager.get_complete_show(search_dialog.chosen_show)
        search_dialog.destroy()

    def _get_show_complete_cb(self, series_manager, show, error):
        if error:
            error_message = ''
            if 'socket' in str(error).lower():
                error_message = '\n ' + _('Please verify your internet connection '
                                          'is available')
                show_information(self,
                                 _('An error occurred. %s') % error_message)
        else:
            self.shows_view.set_shows(self.series_manager.series_list)
            self._update_delete_menu_visibility()
        hildon.hildon_gtk_window_set_progress_indicator(self, False)

    def _row_activated_cb(self, view, path, column):
        show = self.shows_view.get_show_from_path(path)
        seasons_view = SeasonsView(self.settings, self.series_manager, self.connection_manager, show)
        seasons_view.connect('delete-event',
                     lambda w, e:
                        self.shows_view.update(show))
        seasons_view.show_all()
        self.live_search.hide()

    def _long_press_cb(self, widget, path, column):
        show = self.shows_view.get_show_from_path(path)
        dialog = ShowContextDialog(show, self)
        response = dialog.run()
        dialog.destroy()
        if response == ShowContextDialog.INFO_RESPONSE:
            dialog = ShowInfoDialog(show,
                                    title = show.name,
                                    parent = self)
            dialog.run()
            dialog.destroy()
        elif response == ShowContextDialog.DELETE_RESPONSE:
            dialog = gtk.Dialog(title = _('Delete Show'),
                                parent = self,
                                buttons = (gtk.STOCK_NO, gtk.RESPONSE_NO,
                                           gtk.STOCK_YES, gtk.RESPONSE_YES))
            label = gtk.Label(_('Are you sure you want to delete '
                                'the show:\n %(show_name)s' % \
                                {'show_name': show.name}))
            label.show()
            dialog.vbox.add(label)
            response = dialog.run()
            if response == gtk.RESPONSE_YES:
                self.series_manager.delete_show(show)
            dialog.destroy()
        elif response == ShowContextDialog.MARK_NEXT_EPISODE_RESPONSE:
            episodes_info = show.get_episodes_info()
            next_episode = episodes_info['next_episode']
            if next_episode:
                next_episode.watched = True
                next_episode.updated()
                self.shows_view.update(show)
        elif response == ShowContextDialog.UPDATE_RESPONSE:
            hildon.hildon_gtk_window_set_progress_indicator(self, True)
            self.series_manager.update_show_episodes(show)

    def _new_show_dialog(self):
        new_show_dialog = NewShowDialog(self)
        response = new_show_dialog.run()
        if response == gtk.RESPONSE_ACCEPT:
            show_info = new_show_dialog.get_info()
            show = Show(show_info['name'])
            show.overview = show_info['overview']
            show.genre = show_info['genre']
            show.network = show_info['network']
            show.rating = show_info['rating']
            show.actors = show_info['actors']
            self.series_manager.add_show(show)
        new_show_dialog.destroy()

    def _exit_cb(self, window, event):
        if self.request:
            self.request.stop()
        hildon.hildon_gtk_window_set_progress_indicator(self, True)
        # If the shows list is empty but the user hasn't deleted
        # any, then we don't save in order to avoid overwriting
        # the current db (for the shows list might be empty due
        # to an error)
        if not self.series_manager.series_list and not self._have_deleted:
            gtk.main_quit()
            return
        self.series_manager.auto_save(False)

        save_shows_item = AsyncItem(self.series_manager.save,
                               (constants.SF_DB_FILE,))
        save_conf_item = AsyncItem(self.settings.save,
                               (constants.SF_CONF_FILE,),
                               self._save_finished_cb)
        async_worker = AsyncWorker(False)
        async_worker.queue.put(save_shows_item)
        async_worker.queue.put(save_conf_item)
        async_worker.start()

    def _save_finished_cb(self, dummy_arg, error):
        hildon.hildon_gtk_window_set_progress_indicator(self, False)
        gtk.main_quit()

    def _show_list_changed_cb(self, series_manager):
        self.shows_view.set_shows(self.series_manager.series_list)
        self._update_delete_menu_visibility()
        return False

    def _update_delete_menu_visibility(self):
        if not self.series_manager.series_list or self.request:
            self.delete_menu.hide()
            self.update_all_menu.hide()
        else:
            self.delete_menu.show()
            self._on_connection_changed(self.connection_manager)

    def _update_all_shows_cb(self, button):
        hildon.hildon_gtk_window_set_progress_indicator(self, True)
        self.request = self.series_manager.update_all_shows_episodes()
        self.set_sensitive(False)
        self._update_delete_menu_visibility()

    def _update_all_shows_complete_cb(self, series_manager, show, error):
        self._show_list_changed_cb(self.series_manager)
        if self.request:
            if error:
                show_information(self, _('Please verify your internet connection '
                                         'is available'))
            else:
                show_information(self, _('Finished updating the shows'))
        self.request = None
        self.set_sensitive(True)
        self._update_delete_menu_visibility()
        hildon.hildon_gtk_window_set_progress_indicator(self, False)

    def _update_show_complete_cb(self, series_manager, show, error):
        show_information(self, _('Updated "%s"') % show.name)

    def _update_show_art(self, series_manager, show):
        self.shows_view.update_art(show)

    def _about_menu_clicked_cb(self, menu):
        about_dialog = AboutDialog(self)
        about_dialog.set_logo(constants.SF_ICON)
        about_dialog.set_name(constants.SF_NAME)
        about_dialog.set_version(constants.SF_VERSION)
        about_dialog.set_comments(constants.SF_DESCRIPTION)
        about_dialog.set_authors(constants.SF_AUTHORS)
        about_dialog.set_copyright(constants.SF_COPYRIGHT)
        about_dialog.set_license(saxutils.escape(constants.SF_LICENSE))
        about_dialog.run()
        about_dialog.destroy()

    def _settings_menu_clicked_cb(self, menu):
        settings_dialog = SettingsDialog(self)
        response = settings_dialog.run()
        settings_dialog.destroy()
        if response == gtk.RESPONSE_ACCEPT:
            self._applyRotation()

    def _applyRotation(self):
        configured_mode = self.settings.getConf(Settings.SCREEN_ROTATION)
        modes = [self._rotation_manager.AUTOMATIC,
                 self._rotation_manager.ALWAYS,
                 self._rotation_manager.NEVER]
        self._rotation_manager.set_mode(modes[configured_mode])

    def _key_press_event_cb(self, window, event):
        char = gtk.gdk.keyval_to_unicode(event.keyval)
        if self.live_search.is_focus() or char == 0 or not chr(char).strip():
            return
        self.live_search.show()

    def _on_connection_changed(self, connection_manager):
        if connection_manager.is_online():
            self.update_all_menu.show()
        else:
            self.update_all_menu.hide()

class DeleteView(hildon.StackableWindow):

    def __init__(self,
                 tree_view,
                 toolbar_title = _('Delete'),
                 button_label = _('Delete')):
        super(DeleteView, self).__init__()
        self.tree_view = tree_view
        hildon.hildon_gtk_tree_view_set_ui_mode(self.tree_view, gtk.HILDON_UI_MODE_EDIT)
        self.tree_view.get_selection().set_mode(gtk.SELECTION_MULTIPLE)
        shows_area = hildon.PannableArea()
        shows_area.add(self.tree_view)
        self.add(shows_area)

        self.toolbar = hildon.EditToolbar()
        self.toolbar.set_label(toolbar_title)
        self.toolbar.set_button_label(button_label)
        self.toolbar.connect('arrow-clicked', lambda toolbar: self.destroy())
        self.set_edit_toolbar(self.toolbar)

        self.fullscreen()

class ShowsDeleteView(DeleteView):

    def __init__(self, series_manager):
        self.shows_select_view = ShowsSelectView()
        super(ShowsDeleteView, self).__init__(self.shows_select_view,
                                               _('Delete shows'),
                                               _('Delete'))
        self.series_manager = series_manager
        self.toolbar.connect('button-clicked',
                             self._button_clicked_cb)

    def _button_clicked_cb(self, button):
        selection = self.shows_select_view.get_selection()
        selected_rows = selection.get_selected_rows()
        model, paths = selected_rows
        if not paths:
            show_information(self, _('Please select one or more shows'))
            return
        for path in paths:
            self.series_manager.delete_show(model[path][ShowListStore.SHOW_COLUMN])
        self.destroy()

class ShowsSelectView(EnhancedTreeView):

    def __init__(self):
        super(ShowsSelectView, self).__init__()
        self.tree_model = ShowListStore()
        show_image_renderer = gtk.CellRendererPixbuf()
        column = gtk.TreeViewColumn('Image', show_image_renderer,
                                    pixbuf = ShowListStore.IMAGE_COLUMN)
        self.append_column(column)
        show_renderer = gtk.CellRendererText()
        show_renderer.set_property('ellipsize', pango.ELLIPSIZE_END)
        column = gtk.TreeViewColumn('Name', show_renderer, markup = ShowListStore.INFO_COLUMN)
        self.tree_filter = self.tree_model.filter_new()
        self.set_model(self.tree_filter)
        self.append_column(column)

    def set_shows(self, shows):
        self.tree_model.add_shows(shows)
        gobject.idle_add(self.sort)

    def get_show_from_path(self, path):
        return self.get_model()[path][ShowListStore.SHOW_COLUMN]

    def sort_by_recent_date(self):
        self.tree_model.set_sort_column_id(self.tree_model.NEXT_EPISODE_COLUMN,
                                           gtk.SORT_ASCENDING)
        Settings().setConf(Settings.SHOWS_SORT, Settings.RECENT_EPISODE)

    def sort_by_name_ascending(self):
        self.tree_model.set_sort_column_id(self.tree_model.INFO_COLUMN,
                                           gtk.SORT_ASCENDING)
        Settings().setConf(Settings.SHOWS_SORT, Settings.ASCENDING_ORDER)

    def update(self, show = None):
        if self.tree_model:
            self.tree_model.update(show)
            self.sort()

    def update_art(self, show = None):
        if self.tree_model:
            self.tree_model.update_pixmaps(show)

    def sort(self):
        shows_sort_order = Settings().getConf(Settings.SHOWS_SORT)
        if shows_sort_order == Settings.RECENT_EPISODE:
            self.sort_by_recent_date()
        else:
            self.sort_by_name_ascending()

class ShowListStore(gtk.ListStore):

    IMAGE_COLUMN = 0
    INFO_COLUMN = 1
    SHOW_COLUMN = 2
    SEARCH_COLUMN = 3
    NEXT_EPISODE_COLUMN = 4

    def __init__(self):
        super(ShowListStore, self).__init__(gtk.gdk.Pixbuf, str, gobject.TYPE_PYOBJECT, str, gobject.TYPE_PYOBJECT)
        self.cached_pixbufs = {}
        self.downloading_pixbuf = get_downloading_pixbuf()
        self.set_sort_func(self.NEXT_EPISODE_COLUMN, self._sort_func)

    def add_shows(self, shows):
        self.clear()
        for show in shows:
            escaped_name = saxutils.escape(show.name)
            row = {self.IMAGE_COLUMN: self.downloading_pixbuf,
                   self.INFO_COLUMN: escaped_name,
                   self.SHOW_COLUMN: show,
                   self.SEARCH_COLUMN: saxutils.escape(show.name).lower(),
                   self.NEXT_EPISODE_COLUMN: None
                  }
            self.append(row.values())
        self.update(None)
        self.update_pixmaps()

    def update(self, show):
        iter = self.get_iter_first()
        while iter:
            current_show = self.get_value(iter, self.SHOW_COLUMN)
            if show is None or show == current_show:
                self._update_iter(iter)
            iter = self.iter_next(iter)

    def _update_iter(self, iter):
        show = self.get_value(iter, self.SHOW_COLUMN)
        info = show.get_episodes_info()
        info_markup = show.get_info_markup(info)
        self.set_value(iter, self.INFO_COLUMN, info_markup)
        self.set_value(iter, self.NEXT_EPISODE_COLUMN,
                       info['next_episode'])
        self.set_value(iter, self.SEARCH_COLUMN, show.name.lower())

    def _load_pixmap_async(self, show, pixbuf):
        if pixbuf_is_cover(pixbuf):
            return
        if show.image and os.path.isfile(show.image):
            pixbuf = self.cached_pixbufs.get(show.image)
            if not pixbuf:
                try:
                    pixbuf = gtk.gdk.pixbuf_new_from_file_at_size(show.image,
                                                          constants.IMAGE_WIDTH,
                                                          constants.IMAGE_HEIGHT)
                except:
                    pixbuf = get_placeholder_pixbuf()
            self.cached_pixbufs[show.image] = pixbuf
        elif show.downloading_show_image:
            pixbuf = self.downloading_pixbuf
        else:
            pixbuf = get_placeholder_pixbuf()
        return pixbuf

    def _load_pixmap_async_finished(self, show, pixbuf, error):
        if error or not pixbuf:
            return
        iter = self._get_iter_for_show(show)
        if iter:
            self.set_value(iter, self.IMAGE_COLUMN, pixbuf)

    def update_pixmaps(self, show = None):
        iter = self.get_iter_first()
        async_worker = AsyncWorker(True)
        while iter:
            current_show = self.get_value(iter, self.SHOW_COLUMN)
            same_show = show == current_show
            if show is None or same_show:
                pixbuf = self.get_value(iter, self.IMAGE_COLUMN)
                async_item = AsyncItem(self._load_pixmap_async,
                                       (current_show, pixbuf),
                                       self._load_pixmap_async_finished,
                                       (current_show,))
                async_worker.queue.put(async_item)
                if same_show:
                    break
            iter = self.iter_next(iter)
        async_worker.start()

    def _sort_func(self, model, iter1, iter2):
        episode1 = model.get_value(iter1, self.NEXT_EPISODE_COLUMN)
        episode2 = model.get_value(iter2, self.NEXT_EPISODE_COLUMN)
        if not episode1:
            if episode2:
                return 1
            return 0
        if not episode2:
            if episode1:
                return -1
        most_recent = (episode1 or episode2).get_most_recent(episode2)
        if not most_recent:
            return 0
        if episode1 == most_recent:
            return -1
        return 1

    def _get_iter_for_show(self, show):
        if not show:
            return None
        iter = self.get_iter_first()
        while iter:
            current_show = self.get_value(iter, self.SHOW_COLUMN)
            if show == current_show:
                break
            iter = self.iter_next(iter)
        return iter

class SeasonsView(hildon.StackableWindow):

    def __init__(self, settings, series_manager, connection_manager, show):
        super(SeasonsView, self).__init__()
        self.set_title(show.name)

        self.settings = settings

        self.series_manager = series_manager
        self.series_manager.connect('update-show-episodes-complete',
                                    self._update_show_episodes_complete_cb)
        self.series_manager.connect('updated-show-art',
                                    self._update_show_art)

        self.connection_manager = connection_manager
        self.connection_manager.connect('connection-changed',
                                        self._on_connection_changed)
        self.show = show
        self.set_app_menu(self._create_menu())
        self.set_title(show.name)

        self.seasons_select_view = SeasonSelectView(self.show)
        seasons = self.show.get_seasons()
        self.seasons_select_view.set_seasons(seasons)
        self.seasons_select_view.connect('row-activated', self._row_activated_cb)
        self.seasons_select_view.connect('long-press', self._long_press_cb)
        self.connect('delete-event', self._delete_event_cb)

        seasons_area = hildon.PannableArea()
        seasons_area.add(self.seasons_select_view)
        self.add(seasons_area)

        if self.settings.getConf(self.settings.SEASONS_ORDER_CONF_NAME) == \
           self.settings.ASCENDING_ORDER:
            self._sort_ascending_cb(None)
        else:
            self._sort_descending_cb(None)

        self.request = None
        self._update_menu_visibility()

    def _delete_event_cb(self, window, event):
        if self.request:
            self.request.stop()
            self.request = None
        return False

    def _row_activated_cb(self, view, path, column):
        season = self.seasons_select_view.get_season_from_path(path)
        episodes_view = EpisodesView(self.settings, self.show, season)
        episodes_view.connect('delete-event', self._update_series_list_cb)
        episodes_view.connect('episode-list-changed', self._update_series_list_cb)
        episodes_view.show_all()

    def _long_press_cb(self, widget, path, column):
        season = self.seasons_select_view.get_season_from_path(path)
        context_dialog = SeasonContextDialog(self.show, season, self)
        response = context_dialog.run()
        context_dialog.destroy()
        if response == SeasonContextDialog.MARK_EPISODES_RESPONSE:
            self.show.mark_all_episodes_as_watched(season)
        elif response == SeasonContextDialog.UNMARK_EPISODES_RESPONSE:
            self.show.mark_all_episodes_as_not_watched(season)
        elif response == SeasonContextDialog.DELETE_RESPONSE:
            dialog = gtk.Dialog(title = _('Delete Season'),
                                parent = self,
                                buttons = (gtk.STOCK_NO, gtk.RESPONSE_NO,
                                           gtk.STOCK_YES, gtk.RESPONSE_YES))
            label = gtk.Label(_('Are you sure you want to delete '
                                'this season?'))
            label.show()
            dialog.vbox.add(label)
            response = dialog.run()
            if response == gtk.RESPONSE_YES:
                self.show.delete_season(season)
            dialog.destroy()
        seasons = self.show.get_seasons();
        self.seasons_select_view.set_seasons(seasons)

    def _update_series_list_cb(self, widget, event = None):
        seasons = self.show.get_seasons();
        self.seasons_select_view.set_seasons(seasons)
        self._update_menu_visibility()

    def _create_menu(self):
        menu = hildon.AppMenu()

        button_asc = hildon.GtkRadioButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button_asc.set_mode(False)
        button_asc.set_label(_('A-Z'))
        menu.add_filter(button_asc)
        button_desc = hildon.GtkRadioButton(gtk.HILDON_SIZE_FINGER_HEIGHT,
                                            group = button_asc)
        button_desc.set_mode(False)
        button_desc.set_label(_('Z-A'))
        menu.add_filter(button_desc)
        if self.settings.getConf(Settings.SEASONS_ORDER_CONF_NAME) == \
                Settings.ASCENDING_ORDER:
            button_asc.set_active(True)
        else:
            button_desc.set_active(True)
        button_asc.connect('clicked', self._sort_ascending_cb)
        button_desc.connect('clicked', self._sort_descending_cb)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Info'))
        button.connect('clicked', self._show_info_cb)
        menu.append(button)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Edit info'))
        button.connect('clicked', self._edit_show_info)
        menu.append(button)

        self.update_menu = None
        if str(self.show.thetvdb_id) != '-1':
            self.update_menu = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
            self.update_menu.set_label(_('Update show'))
            self.update_menu.connect('clicked', self._update_series_cb)
            menu.append(self.update_menu)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Delete season'))
        button.connect('clicked', self._delete_seasons_cb)
        menu.append(button)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('New episode'))
        button.connect('clicked', self._new_episode_cb)
        menu.append(button)

        menu.show_all()
        return menu

    def _update_menu_visibility(self):
        if not self.update_menu:
            return
        if self.request or not self.show.get_seasons():
            self.update_menu.hide()
        else:
            self.update_menu.show()
            self._on_connection_changed(self.connection_manager)

    def _update_series_cb(self, button):
        self.request = self.series_manager.update_show_episodes(self.show)
        hildon.hildon_gtk_window_set_progress_indicator(self, True)
        self.set_sensitive(False)
        show_information(self, _('Updating show. Please wait...'))
        self._update_menu_visibility()

    def _show_info_cb(self, button):
        dialog = ShowInfoDialog(parent = self)
        dialog.run()
        dialog.destroy()

    def _edit_show_info(self, button):
        edit_series_dialog = EditShowsDialog(self, self.show)
        response = edit_series_dialog.run()
        info = edit_series_dialog.get_info()
        edit_series_dialog.destroy()
        if response == gtk.RESPONSE_ACCEPT:
            self.show.name = info['name']
            self.show.overview = info['overview']
            self.show.genre = info['genre']
            self.show.network = info['network']
            self.show.rating = info['rating']
            self.show.actors = info['actors']
            self.series_manager.updated()
        self.set_title(self.show.name)

    def _new_episode_cb(self, button):
        new_episode_dialog = NewEpisodeDialog(self,
                                              self.show)
        response = new_episode_dialog.run()
        if response == gtk.RESPONSE_ACCEPT:
            episode_info = new_episode_dialog.get_info()
            episode = Episode(episode_info['name'],
                              self.show,
                              episode_info['number'])
            episode.overview = episode_info['overview']
            episode.season_number = episode_info['season']
            episode.episode_number = episode_info['number']
            episode.director = episode_info['director']
            episode.writer = episode_info['writer']
            episode.rating = episode_info['rating']
            episode.air_date = episode_info['air_date']
            episode.guest_stars = episode_info['guest_stars']
            self.show.update_episode_list([episode])
            seasons = self.show.get_seasons()
            self.seasons_select_view.set_seasons(seasons)
        new_episode_dialog.destroy()

    def _update_show_episodes_complete_cb(self, series_manager, show, error):
        if error and self.request:
            error_message = ''
            if 'socket' in str(error).lower():
                error_message = '\n ' + _('Please verify your internet connection '
                                          'is available')
            show_information(self, error_message)
        elif show == self.show:
            seasons = self.show.get_seasons()
            model = self.seasons_select_view.get_model()
            if model:
                model.clear()
                self.seasons_select_view.set_seasons(seasons)
        hildon.hildon_gtk_window_set_progress_indicator(self, False)
        self.set_sensitive(True)
        self.request = None
        self._update_menu_visibility()

    def _update_show_art(self, series_manager, show):
        if show == self.show:
            self.seasons_select_view.update()

    def _delete_seasons_cb(self, button):
        seasons_delete_view = SeasonsDeleteView(self.series_manager,
                                                self.seasons_select_view)
        seasons = self.show.get_seasons()
        seasons_delete_view.show_all()

    def _on_connection_changed(self, connection_manager):
        if connection_manager.is_online():
            self.update_menu.show()
        else:
            self.update_menu.hide()

    def _sort_ascending_cb(self, button):
        self.seasons_select_view.sort_ascending()
        self.settings.setConf(self.settings.SEASONS_ORDER_CONF_NAME,
                              self.settings.ASCENDING_ORDER)

    def _sort_descending_cb(self, button):
        self.seasons_select_view.sort_descending()
        self.settings.setConf(self.settings.SEASONS_ORDER_CONF_NAME,
                              self.settings.DESCENDING_ORDER)

class ShowInfoDialog(gtk.Dialog):

    def __init__(self, show, title = '', parent = None):
        gtk.Dialog.__init__(self, title = title, parent = parent)
        self.show = show
        self.set_title(_('Show details'))
        infotextview = InfoTextView()
        infotextview.set_title(self.show.name)
        infotextview.add_field (self.show.overview)
        infotextview.add_field ('\n')
        infotextview.add_field (self.show.genre, _('Genre'))
        infotextview.add_field (self.show.network, _('Network'))
        infotextview.add_field (self.show.actors, _('Actors'))
        infotextview.add_field (self.show.rating, _('Rating'))
        info_area = hildon.PannableArea()
        info_area.add_with_viewport(infotextview)
        info_area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)
        info_area.set_size_request(-1, 800)
        self.vbox.add(info_area)
        self.vbox.show_all()

class SeasonsDeleteView(DeleteView):

    def __init__(self, series_manager, seasons_select_view):
        self.seasons_select_view = SeasonSelectView(seasons_select_view.show)
        self.seasons_select_view.set_model(seasons_select_view.get_model())
        super(SeasonsDeleteView, self).__init__(self.seasons_select_view,
                                               _('Delete seasons'),
                                               _('Delete'))
        self.series_manager = series_manager
        self.toolbar.connect('button-clicked',
                             self._button_clicked_cb)

    def _button_clicked_cb(self, button):
        selection = self.seasons_select_view.get_selection()
        selected_rows = selection.get_selected_rows()
        model, paths = selected_rows
        if not paths:
            show_information(self, _('Please select one or more seasons'))
            return
        seasons = [model[path][SeasonListStore.SEASON_COLUMN] for path in paths]
        for season in seasons:
            self.seasons_select_view.show.delete_season(season)
            model.delete_season(season)
        self.destroy()

class SeasonListStore(gtk.ListStore):

    IMAGE_COLUMN = 0
    INFO_COLUMN = 1
    SEASON_COLUMN = 2

    def __init__(self, show):
        super(SeasonListStore, self).__init__(gtk.gdk.Pixbuf,
                                              str,
                                              str)
        self.show = show

    def add(self, season_list):
        self.clear()
        for season in season_list:
            if season == '0':
                name = _('Special')
            else:
                name = _('Season %s') % season
            row = {self.IMAGE_COLUMN: None,
                   self.INFO_COLUMN: name,
                   self.SEASON_COLUMN: season,
                  }
            self.append(row.values())
        self.update()

    def update(self):
        iter = self.get_iter_first()
        while iter:
            self._update_iter(iter)
            iter = self.iter_next(iter)

    def delete_season(self, season):
        iter = self.get_iter_first()
        while iter:
            if self.get_value(iter, self.SEASON_COLUMN) == season:
                self.remove(iter)
                break
            iter = self.iter_next(iter)

    def _update_iter(self, iter):
        season = self.get_value(iter, self.SEASON_COLUMN)
        info = self.show.get_season_info_markup(season)
        self.set_value(iter, self.INFO_COLUMN, info)
        pixbuf = self.get_value(iter, self.IMAGE_COLUMN)
        image = self.show.season_images.get(season)
        if pixbuf_is_cover(pixbuf):
            return
        if image and os.path.isfile(image):
            try:
                pixbuf = gtk.gdk.pixbuf_new_from_file_at_size(image,
                                                          constants.IMAGE_WIDTH,
                                                          constants.IMAGE_HEIGHT)
            except:
                pixbuf = get_placeholder_pixbuf()
            self.set_value(iter, self.IMAGE_COLUMN, pixbuf)
        elif self.show.downloading_season_image:
            pixbuf = get_downloading_pixbuf()
            self.set_value(iter, self.IMAGE_COLUMN, pixbuf)
        else:
            pixbuf = get_placeholder_pixbuf()
            self.set_value(iter, self.IMAGE_COLUMN, pixbuf)

class SeasonSelectView(EnhancedTreeView):

    def __init__(self, show):
        super(SeasonSelectView, self).__init__()
        self.show = show
        model = SeasonListStore(self.show)
        season_image_renderer = gtk.CellRendererPixbuf()
        column = gtk.TreeViewColumn('Image', season_image_renderer, pixbuf = model.IMAGE_COLUMN)
        self.append_column(column)
        season_renderer = gtk.CellRendererText()
        season_renderer.set_property('ellipsize', pango.ELLIPSIZE_END)
        column = gtk.TreeViewColumn('Name', season_renderer, markup = model.INFO_COLUMN)
        self.set_model(model)
        self.append_column(column)
        self.get_model().set_sort_func(SeasonListStore.SEASON_COLUMN, self._sort_func)

    def set_seasons(self, season_list):
        model = self.get_model()
        model.add(season_list)

    def get_season_from_path(self, path):
        model = self.get_model()
        iter = model.get_iter(path)
        season = model.get_value(iter, model.SEASON_COLUMN)
        return season

    def update(self):
        model = self.get_model()
        if model:
            model.update()

    def _sort_func(self, model, iter1, iter2):
        season1 = model.get_value(iter1, SeasonListStore.SEASON_COLUMN)
        season2 = model.get_value(iter2, SeasonListStore.SEASON_COLUMN)
        if season1 == None or season2 == None:
            return 0
        if int(season1) < int(season2):
            return -1
        return 1

    def sort_descending(self):
        model = self.get_model()
        model.set_sort_column_id(SeasonListStore.SEASON_COLUMN,
                                 gtk.SORT_DESCENDING)

    def sort_ascending(self):
        model = self.get_model()
        model.set_sort_column_id(SeasonListStore.SEASON_COLUMN,
                                 gtk.SORT_ASCENDING)

class SeasonContextDialog(gtk.Dialog):

    MARK_EPISODES_RESPONSE = 1 << 0
    UNMARK_EPISODES_RESPONSE = 1 << 1
    DELETE_RESPONSE = 1 << 2

    def __init__(self, show, season, parent):
        super(SeasonContextDialog, self).__init__(parent = parent)
        self.show = show
        self.season = season
        season_name = self.season
        if self.season == '0':
            season_name = _('Special')
        self.set_title(_('Season: %(season)s') % {'season': season_name})

        box = gtk.HBox(True)
        mark_episodes_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        if self.show.is_completely_watched(self.season):
            mark_episodes_button.set_label(_('Unmark All Episodes'))
            mark_episodes_button.connect('clicked',
                            lambda b: self.response(self.UNMARK_EPISODES_RESPONSE))
        else:
            mark_episodes_button.set_label(_('Mark All Episodes'))
            mark_episodes_button.connect('clicked',
                            lambda b: self.response(self.MARK_EPISODES_RESPONSE))
        box.add(mark_episodes_button)
        delete_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        delete_button.set_label(_('Delete'))
        delete_button.connect('clicked',
                            lambda b: self.response(self.DELETE_RESPONSE))
        box.add(delete_button)
        self.vbox.add(box)
        self.vbox.show_all()

class ShowContextDialog(gtk.Dialog):

    INFO_RESPONSE = 1 << 0
    MARK_NEXT_EPISODE_RESPONSE = 1 << 1
    UPDATE_RESPONSE = 1 << 2
    DELETE_RESPONSE = 1 << 3

    def __init__(self, show, parent):
        super(ShowContextDialog, self).__init__(parent = parent)
        self.show = show
        self.set_title(self.show.name)

        box = gtk.HBox(True)
        info_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        info_button.set_label('Info')
        info_button.connect('clicked',
                            lambda b: self.response(self.INFO_RESPONSE))
        box.add(info_button)
        if not show.is_completely_watched():
            mark_next_ep_as_watched_button = \
                hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
            mark_next_ep_as_watched_button.set_label('Mark Next Episode')
            mark_next_ep_as_watched_button.connect('clicked',
                       lambda b: self.response(self.MARK_NEXT_EPISODE_RESPONSE))
            box.add(mark_next_ep_as_watched_button)

        online = ConnectionManager().is_online()
        if online or len(box.get_children()) > 1:
            self.vbox.add(box)
            box = gtk.HBox(True)
        if online:
            update_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
            update_button.set_label(_('Update'))
            update_button.connect('clicked',
                                  lambda b: self.response(self.UPDATE_RESPONSE))
            box.add(update_button)
        delete_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        delete_button.set_label(_('Delete'))
        delete_button.connect('clicked',
                       lambda b: self.response(self.DELETE_RESPONSE))
        box.add(delete_button)
        self.vbox.add(box)
        self.vbox.show_all()

class NewShowDialog(gtk.Dialog):

    def __init__(self, parent):
        super(NewShowDialog, self).__init__(parent = parent,
                                             buttons = (gtk.STOCK_ADD,
                                                        gtk.RESPONSE_ACCEPT))

        self.set_title(_('Edit show'))

        self.show_name = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.show_overview = hildon.TextView()
        self.show_overview.set_placeholder(_('Overview'))
        self.show_overview.set_wrap_mode(gtk.WRAP_WORD)
        self.show_genre = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.show_network = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.show_rating = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.show_actors = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)

        contents = gtk.VBox(False, 0)

        row = gtk.HBox(False, 12)
        row.pack_start(gtk.Label(_('Name:')), False, False, 0)
        row.pack_start(self.show_name, True, True, 0)
        contents.pack_start(row, False, False, 0)
        contents.pack_start(self.show_overview, False, False, 0)

        fields = [(_('Genre:'), self.show_genre),
                  (_('Network:'), self.show_network),
                  (_('Rating:'), self.show_rating),
                  (_('Actors:'), self.show_actors),
                 ]
        size_group = gtk.SizeGroup(gtk.SIZE_GROUP_BOTH)
        for text, widget in fields:
            row = gtk.HBox(False, 12)
            label = gtk.Label(text)
            size_group.add_widget(label)
            row.pack_start(label, False, False, 0)
            row.pack_start(widget, True, True, 0)
            contents.pack_start(row, False, False, 0)

        contents_area = hildon.PannableArea()
        contents_area.add_with_viewport(contents)
        contents_area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)

        self.vbox.add(contents_area)
        self.vbox.show_all()

    def get_info(self):
        buffer = self.show_overview.get_buffer()
        start_iter = buffer.get_start_iter()
        end_iter = buffer.get_end_iter()
        overview_text = buffer.get_text(start_iter, end_iter)
        info = {'name': self.show_name.get_text(),
                'overview': overview_text,
                'genre': self.show_genre.get_text(),
                'network': self.show_network.get_text(),
                'rating': self.show_rating.get_text(),
                'actors': self.show_actors.get_text()}
        return info

class EditShowsDialog(NewShowDialog):

    def __init__(self, parent, show):
        super(EditShowsDialog, self).__init__(parent)

        self.show_name.set_text(show.name)
        self.show_overview.get_buffer().set_text(show.overview)
        self.show_genre.set_text(str(show.genre))
        self.show_network.set_text(show.network)
        self.show_rating.set_text(show.rating)
        self.show_actors.set_text(str(show.actors))

class NewEpisodeDialog(gtk.Dialog):

    def __init__(self, parent, show):
        super(NewEpisodeDialog, self).__init__(parent = parent,
                                               buttons = (gtk.STOCK_ADD,
                                                          gtk.RESPONSE_ACCEPT))

        self.set_title(_('New episode'))

        self.episode_name = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.episode_overview = hildon.TextView()
        self.episode_overview.set_placeholder(_('Overview'))
        self.episode_overview.set_wrap_mode(gtk.WRAP_WORD)

        self.episode_number = hildon.PickerButton(gtk.HILDON_SIZE_FINGER_HEIGHT,
                                                  hildon.BUTTON_ARRANGEMENT_VERTICAL)
        selector = hildon.TouchSelectorEntry(text = True)
        self.episode_number.set_title(_('Number:'))
        for i in xrange(20):
            selector.append_text(str(i + 1))
        self.episode_number.set_selector(selector)
        self.episode_number.set_active(0)

        self.episode_season = hildon.PickerButton(gtk.HILDON_SIZE_FINGER_HEIGHT,
                                                  hildon.BUTTON_ARRANGEMENT_VERTICAL)
        selector = hildon.TouchSelectorEntry(text = True)
        self.episode_season.set_title(_('Season:'))
        seasons = show.get_seasons()
        for season in seasons:
            selector.append_text(season)
        self.episode_season.set_selector(selector)
        if seasons:
            self.episode_season.set_active(len(seasons) - 1)
        else:
            selector.append_text('1')
            self.episode_season.set_active(0)

        self.episode_director = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.episode_writer = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.episode_air_date = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.episode_rating = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.episode_guest_stars = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)

        contents = gtk.VBox(False, 0)

        row = gtk.HBox(False, 12)
        row.pack_start(gtk.Label(_('Name:')), False, False, 0)
        row.pack_start(self.episode_name, True, True, 0)
        contents.pack_start(row, False, False, 0)
        contents.pack_start(self.episode_overview, False, False, 0)
        row = gtk.HBox(False, 12)
        row.add(self.episode_season)
        row.add(self.episode_number)
        contents.pack_start(row, False, False, 0)

        fields = [(_('Director:'), self.episode_director),
                  (_('Writer:'), self.episode_writer),
                  (_('Original air date:'), self.episode_air_date),
                  (_('Rating:'), self.episode_rating),
                  (_('Guest stars:'), self.episode_guest_stars),
                 ]
        size_group = gtk.SizeGroup(gtk.SIZE_GROUP_BOTH)
        for text, widget in fields:
            row = gtk.HBox(False, 12)
            label = gtk.Label(text)
            size_group.add_widget(label)
            row.pack_start(label, False, False, 0)
            row.pack_start(widget, True, True, 0)
            contents.pack_start(row, False, False, 0)

        contents_area = hildon.PannableArea()
        contents_area.add_with_viewport(contents)
        contents_area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)

        self.vbox.add(contents_area)
        self.vbox.show_all()

    def get_info(self):
        buffer = self.episode_overview.get_buffer()
        start_iter = buffer.get_start_iter()
        end_iter = buffer.get_end_iter()
        overview_text = buffer.get_text(start_iter, end_iter)
        info = {'name': self.episode_name.get_text(),
                'overview': overview_text,
                'season': self.episode_season.get_selector().get_entry().get_text(),
                'number': self.episode_number.get_selector().get_entry().get_text(),
                'director': self.episode_director.get_text(),
                'writer': self.episode_writer.get_text(),
                'air_date': self.episode_air_date.get_text(),
                'rating': self.episode_rating.get_text(),
                'guest_stars': self.episode_guest_stars.get_text()}
        return info

class EditEpisodeDialog(NewEpisodeDialog):

    def __init__(self, parent, episode):
        super(EditEpisodeDialog, self).__init__(parent, episode.show)

        self.episode_name.set_text(episode.name)
        self.episode_overview.get_buffer().set_text(episode.overview)
        self.episode_season.get_selector().get_entry().set_text(episode.season_number)
        self.episode_number.get_selector().get_entry().set_text(str(episode.episode_number))
        self.episode_director.set_text(episode.director)
        self.episode_writer.set_text(str(episode.writer))
        self.episode_air_date.set_text(str(episode.air_date))
        self.episode_rating.set_text(episode.rating)
        self.episode_guest_stars.set_text(str(episode.guest_stars))

class EpisodesView(hildon.StackableWindow):

    EPISODES_LIST_CHANGED_SIGNAL = 'episode-list-changed'

    __gsignals__ = {EPISODES_LIST_CHANGED_SIGNAL: (gobject.SIGNAL_RUN_LAST,
                                                   gobject.TYPE_NONE,
                                                   ()),
                   }

    def __init__(self, settings, show, season_number = None):
        super(EpisodesView, self).__init__()

        self.settings = Settings()
        self.series_manager = SeriesManager()

        self.show = show
        self.season_number = season_number
        self.set_title(self.show.name)
        self.episodes_check_view = EpisodesCheckView()
        self.episodes_check_view.set_episodes(self.show.get_episodes_by_season(self.season_number))
        self.episodes_check_view.watched_renderer.connect('toggled',
                                                          self._watched_renderer_toggled_cb,
                                                          self.episodes_check_view.get_model())
        self.episodes_check_view.connect('row-activated', self._row_activated_cb)

        episodes_area = hildon.PannableArea()
        episodes_area.add(self.episodes_check_view)
        self.add(episodes_area)
        self.set_app_menu(self._create_menu())
        if self.settings.getConf(self.settings.EPISODES_ORDER_CONF_NAME) == \
           self.settings.ASCENDING_ORDER:
            self._sort_ascending_cb(None)
        else:
            self._sort_descending_cb(None)

    def _create_menu(self):
        menu = hildon.AppMenu()

        button_asc = hildon.GtkRadioButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button_asc.set_mode(False)
        button_asc.set_label(_('A-Z'))
        menu.add_filter(button_asc)
        button_desc = hildon.GtkRadioButton(gtk.HILDON_SIZE_FINGER_HEIGHT, group = button_asc)
        button_desc.set_mode(False)
        button_desc.set_label(_('Z-A'))
        menu.add_filter(button_desc)
        if self.settings.getConf(Settings.EPISODES_ORDER_CONF_NAME) == \
                Settings.ASCENDING_ORDER:
            button_asc.set_active(True)
        else:
            button_desc.set_active(True)
        button_asc.connect('clicked', self._sort_ascending_cb)
        button_desc.connect('clicked', self._sort_descending_cb)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Mark all'))
        button.connect('clicked', self._select_all_cb)
        menu.append(button)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Mark none'))
        button.connect('clicked', self._select_none_cb)
        menu.append(button)

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Delete episodes'))
        button.connect('clicked', self._delete_episodes_cb)
        menu.append(button)

        menu.show_all()
        return menu

    def _delete_episodes_cb(self, button):
        delete_episodes_view = EpisodesDeleteView(self.show)
        episodes = self.show.get_episodes_by_season(self.season_number)
        delete_episodes_view.episodes_select_view.set_episodes(episodes)
        delete_episodes_view.toolbar.connect('button-clicked',
                                             self._update_episodes_list_cb)
        delete_episodes_view.show_all()

    def _select_all_cb(self, button):
        self.episodes_check_view.select_all()

    def _select_none_cb(self, button):
        self.episodes_check_view.select_none()

    def _row_activated_cb(self, view, path, column):
        episode = self.episodes_check_view.get_episode_from_path(path)
        if self.episodes_check_view.get_column(EpisodeListStore.INFO_COLUMN) == column:
            episodes_view = EpisodeView(episode)
            episodes_view.connect('delete-event', self._update_episodes_list_cb)
            episodes_view.show_all()

    def _update_episodes_list_cb(self, widget, event = None):
        self.emit(self.EPISODES_LIST_CHANGED_SIGNAL)
        episodes = self.show.get_episodes_by_season(self.season_number)
        if episodes:
            self.episodes_check_view.set_episodes(episodes)
        else:
            self.destroy()
        return False

    def _watched_renderer_toggled_cb(self, renderer, path, model):
        episode = self.episodes_check_view.get_episode_from_path(path)
        episode.watched = not episode.watched
        episode.updated()
        model[path][model.CHECK_COLUMN] = episode.watched
        model.update_iter(model.get_iter(path))

    def _sort_ascending_cb(self, button):
        self.episodes_check_view.sort_ascending()
        self.settings.setConf(self.settings.EPISODES_ORDER_CONF_NAME,
                              self.settings.ASCENDING_ORDER)

    def _sort_descending_cb(self, button):
        self.episodes_check_view.sort_descending()
        self.settings.setConf(self.settings.EPISODES_ORDER_CONF_NAME,
                              self.settings.DESCENDING_ORDER)

class EpisodeListStore(gtk.ListStore):
    CHECK_COLUMN = 0
    INFO_COLUMN = 1
    EPISODE_COLUMN = 2

    def __init__(self):
        EpisodeListStore.CHECK_COLUMN = Settings().getConf(Settings.EPISODES_CHECK_POSITION)
        EpisodeListStore.INFO_COLUMN = 1 - EpisodeListStore.CHECK_COLUMN
        types = {self.CHECK_COLUMN: bool, self.INFO_COLUMN: str,
                 self.EPISODE_COLUMN: gobject.TYPE_PYOBJECT}
        super(EpisodeListStore, self).__init__(*types.values())

    def add(self, episode_list):
        self.clear()
        for episode in episode_list:
            name = str(episode)
            row = {self.CHECK_COLUMN: episode.watched,
                   self.INFO_COLUMN: saxutils.escape(str(name)),
                   self.EPISODE_COLUMN: episode}
            self.append(row.values())
        self.update()


    def update(self):
        iter = self.get_iter_root()
        while iter:
            next_iter = self.iter_next(iter)
            self.update_iter(iter)
            iter = next_iter

    def update_iter(self, iter):
        episode = self.get_value(iter, self.EPISODE_COLUMN)
        info = episode.get_info_markup()
        self.set_value(iter, self.INFO_COLUMN, info)

class EpisodesCheckView(gtk.TreeView):

    def __init__(self):
        super(EpisodesCheckView, self).__init__()
        model = EpisodeListStore()
        episode_renderer = gtk.CellRendererText()
        episode_renderer.set_property('ellipsize', pango.ELLIPSIZE_END)
        column = gtk.TreeViewColumn('Name', episode_renderer, markup = model.INFO_COLUMN)
        column.set_property('expand', True)
        self.append_column(column)
        self.watched_renderer = gtk.CellRendererToggle()
        self.watched_renderer.set_property('width', 100)
        self.watched_renderer.set_property('activatable', True)
        column = gtk.TreeViewColumn('Watched', self.watched_renderer)
        column.add_attribute(self.watched_renderer, "active", model.CHECK_COLUMN)
        self.insert_column(column, model.CHECK_COLUMN)
        self.set_model(model)
        self.get_model().set_sort_func(2, self._sort_func)

    def _sort_func(self, model, iter1, iter2):
        episode1 = model.get_value(iter1, model.EPISODE_COLUMN)
        episode2 = model.get_value(iter2, model.EPISODE_COLUMN)
        if episode1 == None or episode2 == None:
            return 0
        if episode1.episode_number < episode2.episode_number:
            return -1
        return 1

    def set_episodes(self, episode_list):
        model = self.get_model()
        model.add(episode_list)

    def get_episode_from_path(self, path):
        model = self.get_model()
        iter = model.get_iter(path)
        episode = model.get_value(iter, model.EPISODE_COLUMN)
        return episode

    def sort_descending(self):
        model = self.get_model()
        model.set_sort_column_id(model.EPISODE_COLUMN,
                                 gtk.SORT_DESCENDING)

    def sort_ascending(self):
        model = self.get_model()
        model.set_sort_column_id(model.EPISODE_COLUMN,
                                 gtk.SORT_ASCENDING)

    def select_all(self):
        self._set_episodes_selection(True)
        self.get_model().update()

    def select_none(self):
        self._set_episodes_selection(False)
        self.get_model().update()

    def _set_episodes_selection(self, mark):
        model = self.get_model()
        for path in model or []:
            path[model.CHECK_COLUMN] = \
                path[model.EPISODE_COLUMN].watched = mark
            path[model.EPISODE_COLUMN].updated()

class EpisodeView(hildon.StackableWindow):

    def __init__(self, episode):
        super(EpisodeView, self).__init__()
        self.episode = episode

        self.set_app_menu(self._create_menu())

        contents_area = hildon.PannableArea()
        contents_area.connect('horizontal-movement',
                              self._horizontal_movement_cb)
        contents = gtk.VBox(False, 0)
        contents_area.add_with_viewport(contents)

        self.infotextview = InfoTextView()
        self._update_info_text_view()
        contents.add(self.infotextview)

        self.add(contents_area)

    def _update_info_text_view(self):
        self.infotextview.clear()
        self._set_episode_title()
        self.check_button.set_active(self.episode.watched)
        self.infotextview.add_field(self.episode.overview)
        self.infotextview.add_field('\n')
        self.infotextview.add_field(self.episode.get_air_date_text(),
                                    _('Original air date'))
        self.infotextview.add_field(self.episode.director, _('Director'))
        self.infotextview.add_field(self.episode.writer, _('Writer'))
        self.infotextview.add_field(self.episode.guest_stars, _('Guest stars'))
        self.infotextview.add_field(self.episode.rating, _('Rating'))
        self.set_title(self.episode.name)

    def _set_episode_title(self):
        self.infotextview.set_title('%(number)s - %(name)s' % {'name': self.episode.name,
                                                               'number': self.episode.get_episode_show_number()},
                                    self.episode.watched)

    def _create_menu(self):
        menu = hildon.AppMenu()

        button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        button.set_label(_('Edit info'))
        button.connect('clicked', self._edit_episode_cb)
        menu.append(button)

        self.check_button = hildon.CheckButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.check_button.set_label(_('Watched'))
        self.check_button.set_active(self.episode.watched)
        self.check_button.connect('toggled', self._watched_button_toggled_cb)
        menu.append(self.check_button)

        menu.show_all()
        return menu

    def _edit_episode_cb(self, button):
        edit_episode_dialog = EditEpisodeDialog(self,
                                               self.episode)
        response = edit_episode_dialog.run()
        if response == gtk.RESPONSE_ACCEPT:
            episode_info = edit_episode_dialog.get_info()
            self.episode.name = episode_info['name']
            self.episode.overview = episode_info['overview']
            self.episode.season_number = episode_info['season']
            self.episode.episode_number = episode_info['number']
            self.episode.air_date = episode_info['air_date']
            self.episode.director = episode_info['director']
            self.episode.writer = episode_info['writer']
            self.episode.rating = episode_info['rating']
            self.episode.guest_stars = episode_info['guest_stars']
            self.episode.updated()
            self._update_info_text_view()
        edit_episode_dialog.destroy()

    def _horizontal_movement_cb(self, pannable_area, direction,
                                initial_x, initial_y):
        if direction == hildon.MOVEMENT_LEFT:
            episode = self.episode.show.get_next_episode(self.episode)
        else:
            episode = self.episode.show.get_previous_episode(self.episode)
        if episode:
            self.episode = episode
            self._update_info_text_view()

    def _watched_button_toggled_cb(self, button):
        self.episode.watched = button.get_active()
        self.episode.updated()
        self._set_episode_title()

class EpisodesDeleteView(DeleteView):

    def __init__(self, show):
        self.episodes_select_view = EpisodesSelectView()
        super(EpisodesDeleteView, self).__init__(self.episodes_select_view,
                                                 _('Delete episodes'),
                                                 _('Delete'))
        self.show = show
        self.toolbar.connect('button-clicked',
                             self._button_clicked_cb)

    def _button_clicked_cb(self, button):
        selection = self.episodes_select_view.get_selection()
        selected_rows = selection.get_selected_rows()
        model, paths = selected_rows
        if not paths:
            show_information(self, _('Please select one or more episodes'))
            return
        for path in paths:
            self.show.delete_episode(model[path][1])
        self.destroy()

class EpisodesSelectView(gtk.TreeView):

    def __init__(self):
        super(EpisodesSelectView, self).__init__()
        model = gtk.ListStore(str, gobject.TYPE_PYOBJECT)
        column = gtk.TreeViewColumn('Name', gtk.CellRendererText(), text = 0)
        self.append_column(column)
        self.set_model(model)

    def set_episodes(self, episode_list):
        model = self.get_model()
        model.clear()
        for episode in episode_list:
            name = str(episode)
            model.append([name, episode])

    def get_episode_from_path(self, path):
        model = self.get_model()
        iter = model.get_iter(path)
        episode = model.get_value(iter, 1)
        return episode

class InfoTextView(hildon.TextView):

    TEXT_TAG = 'title'

    def __init__(self):
        super(InfoTextView, self).__init__()

        buffer = gtk.TextBuffer()

        self.set_buffer(buffer)
        self.set_wrap_mode(gtk.WRAP_WORD)
        self.set_editable(False)
        self.set_cursor_visible(False)

    def set_title(self, title, strike = False):
        if not title:
            return
        text_buffer = self.get_buffer()
        tag_table = text_buffer.get_tag_table()
        title_tag = tag_table.lookup(self.TEXT_TAG)
        if not title_tag:
            title_tag = gtk.TextTag(self.TEXT_TAG)
            tag_table.add(title_tag)
        else:
            title_end_iter = text_buffer.get_start_iter()
            if not title_end_iter.forward_to_tag_toggle(title_tag):
                title_end_iter = text_buffer.get_start_iter()
            text_buffer.delete(text_buffer.get_start_iter(),
                               title_end_iter)

        title_tag.set_property('weight', pango.WEIGHT_BOLD)
        title_tag.set_property('size', pango.units_from_double(24.0))
        title_tag.set_property('underline-set', True)
        title_tag.set_property('strikethrough', strike)
        text_buffer.insert_with_tags(text_buffer.get_start_iter(), str(title) + '\n', title_tag)

    def add_field(self, contents, label = None):
        if not contents:
            return
        if label:
            contents = _('\n%(label)s: %(contents)s') % {'label': label,
                                                         'contents': contents,
                                                        }
        self.get_buffer().insert(self.get_buffer().get_end_iter(), contents)

    def clear(self):
        buffer = self.get_buffer()
        if not buffer:
            return
        buffer.delete(buffer.get_start_iter(), buffer.get_end_iter())

class LiveSearchEntry(gtk.HBox):

    def __init__(self, tree_model, tree_filter, filter_column = 0):
        super(LiveSearchEntry, self).__init__()
        self.tree_model = tree_model
        self.tree_filter = tree_filter
        if not self.tree_model or not self.tree_filter:
            return
        self.filter_column = filter_column
        self.tree_filter.set_visible_func(self._tree_filter_func)
        self.entry = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.entry.set_input_mode(gtk.HILDON_GTK_INPUT_MODE_FULL)
        self.entry.show()
        self.entry.connect('changed',
                           self._entry_changed_cb)
        self.cancel_button = gtk.ToolButton()
        image = gtk.image_new_from_icon_name('general_close',
                                             gtk.ICON_SIZE_LARGE_TOOLBAR)
        if image:
            self.cancel_button.set_icon_widget(image)
        else:
            self.cancel_button.set_label(_('Cancel'))
        self.cancel_button.set_size_request(self.cancel_button.get_size_request()[1], -1)
        self.cancel_button.show()
        self.cancel_button.connect('clicked', self._cancel_button_clicked_cb)
        self.pack_start(self.entry)
        self.pack_start(self.cancel_button, False, False)

    def _cancel_button_clicked_cb(self, button):
        self.hide()
        self.entry.set_text('')

    def _tree_filter_func(self, model, iter):
        info = model.get_value(iter, self.filter_column) or ''
        text_to_filter = self.entry.get_text().strip()
        if not text_to_filter:
            return True
        expr = re.compile('(.*\s+)*(%s)' % re.escape(text_to_filter.lower()))
        if expr.match(info):
            return True
        return False

    def _entry_changed_cb(self, entry):
        self.tree_filter.refilter()
        if not entry.get_text():
            self.hide()

    def show(self):
        gtk.HBox.show(self)
        self.entry.grab_focus()

    def hide(self):
        gtk.HBox.hide(self)
        self.entry.set_text('')

class NewShowsDialog(gtk.Dialog):

    ADD_AUTOMATICALLY_RESPONSE = 1 << 0
    ADD_MANUALLY_RESPONSE      = 1 << 1

    def __init__(self, parent):
        super(NewShowsDialog, self).__init__(parent = parent)
        self.set_title(_('Add shows'))
        contents = gtk.HBox(True, 0)
        self.search_shows_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.search_shows_button.set_label(_('Search shows'))
        self.search_shows_button.connect('clicked', self._button_clicked_cb)
        self.manual_add_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.manual_add_button.set_label(_('Add manually'))
        self.manual_add_button.connect('clicked', self._button_clicked_cb)
        contents.add(self.search_shows_button)
        contents.add(self.manual_add_button)
        self.vbox.add(contents)
        self.vbox.show_all()
        parent.connection_manager.connect('connection-changed',
                                          self._on_connection_changed)
        self._on_connection_changed(parent.connection_manager)

    def _button_clicked_cb(self, button):
        if button == self.search_shows_button:
            self.response(self.ADD_AUTOMATICALLY_RESPONSE)
        elif button == self.manual_add_button:
            self.response(self.ADD_MANUALLY_RESPONSE)

    def _on_connection_changed(self, connection_manager):
        if connection_manager.is_online():
            self.search_shows_button.show()
        else:
            self.search_shows_button.hide()

class FoundShowListStore(gtk.ListStore):

    NAME_COLUMN = 0
    MARKUP_COLUMN = 1

    def __init__(self):
        super(FoundShowListStore, self).__init__(str, str)

    def add_shows(self, shows):
        self.clear()
        for name in shows:
            markup_name = saxutils.escape(str(name))
            if self.series_manager.get_show_by_name(name):
                row = {self.NAME_COLUMN: name,
                       self.MARKUP_COLUMN: '<span foreground="%s">%s</span>' % \
                                           (get_color(constants.ACTIVE_TEXT_COLOR), markup_name)}
            else:
                row = {self.NAME_COLUMN: name,
                       self.MARKUP_COLUMN: '<span>%s</span>' % markup_name}
            self.append(row.values())

class SearchShowsDialog(gtk.Dialog):

    def __init__(self, parent, series_manager, settings):
        super(SearchShowsDialog, self).__init__(parent = parent)
        self.set_title(_('Search shows'))

        self.series_manager = series_manager
        self.series_manager.connect('search-shows-complete', self._search_shows_complete_cb)
        self.settings = settings
        self.connect('response', self._response_cb)

        self.chosen_show = None
        self.chosen_lang = None

        self.shows_view = hildon.GtkTreeView(gtk.HILDON_UI_MODE_EDIT)
        model = FoundShowListStore()
        model.series_manager = series_manager
        show_renderer = gtk.CellRendererText()
        show_renderer.set_property('ellipsize', pango.ELLIPSIZE_END)
        column = gtk.TreeViewColumn('Name', show_renderer, markup = model.MARKUP_COLUMN)
        self.shows_view.set_model(model)
        self.shows_view.append_column(column)

        self.search_entry = hildon.Entry(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.search_entry.connect('changed', self._search_entry_changed_cb)
        self.search_entry.connect('activate', self._search_entry_activated_cb)
        self.search_button = hildon.GtkButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        self.search_button.set_label(_('Search'))
        self.search_button.connect('clicked', self._search_button_clicked)
        self.search_button.set_sensitive(False)
        self.search_button.set_size_request(150, -1)
        search_contents = gtk.HBox(False, 0)
        search_contents.pack_start(self.search_entry, True, True, 0)
        search_contents.pack_start(self.search_button, False, False, 0)
        self.vbox.pack_start(search_contents, False, False, 0)

        self.lang_store = gtk.ListStore(str, str);
        for langid, langdesc in self.series_manager.get_languages().iteritems():
            self.lang_store.append([langid, langdesc])
        lang_button = hildon.PickerButton(gtk.HILDON_SIZE_AUTO, hildon.BUTTON_ARRANGEMENT_VERTICAL)
        lang_button.set_title(_('Language'))
        self.lang_selector = hildon.TouchSelector()
        lang_column = self.lang_selector.append_column(self.lang_store, gtk.CellRendererText(), text=1)
        lang_column.set_property("text-column", 1)
        self.lang_selector.set_column_selection_mode(hildon.TOUCH_SELECTOR_SELECTION_MODE_SINGLE)
        lang_button.set_selector(self.lang_selector)
        try:
            self.lang_selector.set_active(0, self.series_manager.get_languages().keys().index(self.settings.getConf(Settings.SEARCH_LANGUAGE)))
        except ValueError:
            pass
        lang_button.connect('value-changed',
                            self._language_changed_cb)

        shows_area = hildon.PannableArea()
        shows_area.add(self.shows_view)
        shows_area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)
        self.vbox.add(shows_area)

        self.action_area.pack_start(lang_button, True, True, 0)
        self.ok_button = self.add_button(gtk.STOCK_OK, gtk.RESPONSE_ACCEPT)
        self.ok_button.set_sensitive(False)
        self.action_area.show_all()

        self.vbox.show_all()
        self.set_size_request(-1, 400)

    def _language_changed_cb(self, button):
        self.settings.setConf(Settings.SEARCH_LANGUAGE,
                              self.lang_store[button.get_active()][0])

    def _search_entry_changed_cb(self, entry):
        enable = self.search_entry.get_text().strip()
        self.search_button.set_sensitive(bool(enable))

    def _search_entry_activated_cb(self, entry):
        self._search()

    def _search_button_clicked(self, button):
        self._search()

    def _search(self):
        self._set_controls_sensitive(False)
        hildon.hildon_gtk_window_set_progress_indicator(self, True)
        search_terms = self.search_entry.get_text()
        if not self.search_entry.get_text():
            return
        selected_row = self.lang_selector.get_active(0)
        if selected_row < 0:
            self.series_manager.search_shows(search_terms)
        else:
            lang = self.lang_store[selected_row][0]
            self.series_manager.search_shows(search_terms, lang)

    def _search_shows_complete_cb(self, series_manager, shows, error):
        if error:
            error_message = ''
            if 'socket' in str(error).lower():
                error_message = '\n ' + _('Please verify your internet connection '
                                          'is available')
                show_information(self, _('An error occurred. %s') % error_message)
        else:
            model = self.shows_view.get_model()
            if not model:
                return
            model.clear()
            if shows:
                model.add_shows(shows)
                self.ok_button.set_sensitive(True)
            else:
                self.ok_button.set_sensitive(False)
        hildon.hildon_gtk_window_set_progress_indicator(self, False)
        self._set_controls_sensitive(True)

    def _set_controls_sensitive(self, sensitive):
        self.search_entry.set_sensitive(sensitive)
        self.search_button.set_sensitive(sensitive)

    def _response_cb(self, dialog, response):
        selection = self.shows_view.get_selection()
        model, paths = selection.get_selected_rows()
        for path in paths:
            iter = model.get_iter(path)
            text = model.get_value(iter, model.NAME_COLUMN)
            self.chosen_show = text
        selected_lang = self.lang_selector.get_active(0)
        if selected_lang >= 0:
            self.chosen_lang = self.lang_store[self.lang_selector.get_active(0)][0]

class AboutDialog(gtk.Dialog):

    PADDING = 5

    def __init__(self, parent):
        super(AboutDialog, self).__init__(parent = parent,
                                       flags = gtk.DIALOG_DESTROY_WITH_PARENT)
        self._logo = gtk.Image()
        self._name = ''
        self._name_label = gtk.Label()
        self._version = ''
        self._comments_label = gtk.Label()
        self._copyright_label = gtk.Label()
        self._license_label = gtk.Label()
        _license_alignment = gtk.Alignment(0, 0, 0, 1)
        _license_alignment.add(self._license_label)
        self._license_label.set_line_wrap(True)

        self._writers_caption = gtk.Label()
        self._writers_caption.set_markup('<b>%s</b>' % _('Authors:'))
        _writers_caption = gtk.Alignment()
        _writers_caption.add(self._writers_caption)
        self._writers_label = gtk.Label()
        self._writers_contents = gtk.VBox(False, 0)
        self._writers_contents.pack_start(_writers_caption)
        _writers_alignment = gtk.Alignment(0.2, 0, 0, 1)
        _writers_alignment.add(self._writers_label)
        self._writers_contents.pack_start(_writers_alignment)

        _contents = gtk.VBox(False, 0)
        _contents.pack_start(self._logo, False, False, self.PADDING)
        _contents.pack_start(self._name_label, False, False, self.PADDING)
        _contents.pack_start(self._comments_label, False, False, self.PADDING)
        _contents.pack_start(self._copyright_label, False, False, self.PADDING)
        _contents.pack_start(self._writers_contents, False, False, self.PADDING)
        _contents.pack_start(_license_alignment, False, False, self.PADDING)

        _contents_area = hildon.PannableArea()
        _contents_area.add_with_viewport(_contents)
        _contents_area.set_size_request_policy(hildon.SIZE_REQUEST_CHILDREN)
        self.vbox.add(_contents_area)
        self.vbox.show_all()
        self._writers_contents.hide()

    def set_logo(self, logo_path):
        self._logo.set_from_file(logo_path)

    def set_name(self, name):
        self._name = name
        self.set_version(self._version)
        self.set_title(_('About %s') % self._name)

    def _set_name_label(self, name):
        self._name_label.set_markup('<big>%s</big>' % name)

    def set_version(self, version):
        self._version = version
        self._set_name_label('%s %s' % (self._name, self._version))

    def set_comments(self, comments):
        self._comments_label.set_text(comments)

    def set_copyright(self, copyright):
        self._copyright_label.set_markup('<small>%s</small>' % copyright)

    def set_license(self, license):
        self._license_label.set_markup('<b>%s</b>\n<small>%s</small>' % \
                                       (_('License:'), license))

    def set_authors(self, authors_list):
        authors = '\n'.join(authors_list)
        self._writers_label.set_text(authors)
        self._writers_contents.show_all()

class SettingsDialog(gtk.Dialog):

    def __init__(self, parent):
        super(SettingsDialog, self).__init__(parent = parent,
                                         title = _('Settings'),
                                         flags = gtk.DIALOG_DESTROY_WITH_PARENT,
                                         buttons = (gtk.STOCK_SAVE,
                                                    gtk.RESPONSE_ACCEPT))
        self.settings = Settings()
        self.vbox.pack_start(self._create_screen_rotation_settings())
        self.vbox.pack_start(self._create_shows_settings())
        self.vbox.pack_start(self._create_episodes_check_settings())
        self.vbox.show_all()

    def _create_screen_rotation_settings(self):
        picker_button = hildon.PickerButton(gtk.HILDON_SIZE_FINGER_HEIGHT,
                                            hildon.BUTTON_ARRANGEMENT_HORIZONTAL)
        picker_button.set_alignment(0, 0.5, 0, 1)
        picker_button.set_done_button_text(_('Done'))
        selector = hildon.TouchSelector(text = True)
        picker_button.set_title(_('Screen rotation:'))
        modes = [_('Automatic'), _('Portrait'), _('Landscape')]
        for mode in modes:
            selector.append_text(mode)
        picker_button.set_selector(selector)
        picker_button.set_active(self.settings.getConf(Settings.SCREEN_ROTATION))
        picker_button.connect('value-changed',
                              self._screen_rotation_picker_button_changed_cb)
        return picker_button

    def _create_shows_settings(self):
        check_button = hildon.CheckButton(gtk.HILDON_SIZE_FINGER_HEIGHT)
        check_button.set_label(_('Add special seasons'))
        check_button.set_active(self.settings.getConf(Settings.ADD_SPECIAL_SEASONS))
        check_button.connect('toggled',
                             self._special_seasons_check_button_toggled_cb)
        return check_button

    def _create_episodes_check_settings(self):
        picker_button = hildon.PickerButton(gtk.HILDON_SIZE_FINGER_HEIGHT,
                                            hildon.BUTTON_ARRANGEMENT_HORIZONTAL)
        picker_button.set_title(_('Episodes check position:'))
        picker_button.set_alignment(0, 0.5, 0, 1)
        selector = hildon.TouchSelector(text = True)
        selector.append_text(_('Left'))
        selector.append_text(_('Right'))
        picker_button.set_selector(selector)
        picker_button.set_active(self.settings.getConf(Settings.EPISODES_CHECK_POSITION))
        picker_button.connect('value-changed',
                              self._episodes_check_picker_button_changed_cb)
        return picker_button

    def _special_seasons_check_button_toggled_cb(self, button):
        self.settings.setConf(Settings.ADD_SPECIAL_SEASONS, button.get_active())

    def _screen_rotation_picker_button_changed_cb(self, button):
        self.settings.setConf(Settings.SCREEN_ROTATION, button.get_active())

    def _episodes_check_picker_button_changed_cb(self, button):
        self.settings.setConf(Settings.EPISODES_CHECK_POSITION,
                              button.get_active())

def show_information(parent, message):
    hildon.hildon_banner_show_information(parent,
                                          '',
                                          message)

def pixbuf_is_cover(pixbuf):
    if pixbuf:
        return not bool(pixbuf.get_data('is_placeholder'))
    return False

def get_downloading_pixbuf():
    pixbuf = gtk.gdk.pixbuf_new_from_file_at_size(constants.DOWNLOADING_IMAGE,
                                                  constants.IMAGE_WIDTH,
                                                  constants.IMAGE_HEIGHT)
    pixbuf.set_data('is_placeholder', True)
    return pixbuf

def get_placeholder_pixbuf():
    pixbuf = gtk.gdk.pixbuf_new_from_file_at_size(constants.PLACEHOLDER_IMAGE,
                                                constants.IMAGE_WIDTH,
                                                constants.IMAGE_HEIGHT)
    pixbuf.set_data('is_placeholder', True)
    return pixbuf
