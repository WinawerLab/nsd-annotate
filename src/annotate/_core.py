# -*- coding: utf-8 -*-
################################################################################
# annotate/_core.py

"""Core implementation code for the annotation tool's interface.

This file primarily contains code for managing the widget and window state of
the control panel; the canvas and figure code is largely handled by the
FigurePanel widget in the _figure.py file.
"""


# Imports ######################################################################

import os
import json
from functools import partial

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib as mpl
import ipywidgets as ipw
import imageio.v3 as iio
import yaml, json

from ._util    import (ldict, delay)
from ._config  import Config
from ._control import ControlPanel
from ._figure  import FigurePanel


# The State Manager ############################################################

class NoOpContext:
    def __enter__(self): pass
    def __exit__(self, type, value, traceback): pass
class AnnotationState:
    """The manager of the state of the annotation and the annotation tool.

    The `AnnotationState` class manages the state of the annotation tool. This
    state includes the cache, the user preferences (style settings), and the
    saved annotations.
    """
    @property
    def gitdata(self):
        """Reads and returns the repo username and the repo name."""
        # If we weren't given a git path, we return standard nothings.
        if self.git_path is None:
            return ('', '')
        try:
            # For some reason, it seems that sometimes docker doesn't fully
            # mount the directory until we've attempted to list its contents.
            with os.popen(f'ls {self.git_path}') as f: f.read()
            # Having performed an ls, go ahead and check git's opinion about the
            # origin.
            cmd = f'cd {self.git_path} && git config --get remote.origin.url'
            with os.popen(cmd) as p:
                repo_url = p.read().strip()
            repo_split = repo_url.split('/')
            repo_name = repo_split.pop()
            while repo_name == '':
                repo_name = repo_split.pop()
            repo_user = repo_split.pop()
            s1 = repo_user.split('/')[-1]
            s2 = repo_user.split(':')[-1]
            repo_user = s1 if len(s1) < len(s2) else s2
            return (repo_user, repo_name)
        except Exception as e:
            from warning import warn
            warn(f"error finding gitdata: {e}")
            return ('', '')
    def target_path(self, target):
        """Returns the relative path for a target."""
        if isinstance(target, tuple):
            path = target
        else:
            path = [target[k] for k in self.config.targets.concrete_keys]
        return os.path.join(*path)
    def target_figure_path(self, target, figure=None, ensure=True):
        """Returns the cache path for a target's figures."""
        path = self.target_path(target)
        path = os.path.join(self.cache_path, 'figures', path)
        if ensure and not os.path.isdir(path):
            os.makedirs(path, mode=0o755)
        if figure is not None:
            path = os.path.join(path, f"{figure}.png")
        return path
    def target_grid_path(self, target, annotation=None, ensure=True):
        """Returns the cache path for a target's grids."""
        path = os.path.join(self.cache_path, 'grids', self.target_path(target))
        if ensure and not os.path.isdir(path):
            os.makedirs(path, mode=0o755)
        if annotation is not None:
            path = os.path.join(path, f"{annotation}.png")
        return path
    def target_save_path(self, target, annotation=None, ensure=True):
        """Returns the save path for a target's annotation data."""
        path = os.path.join(self.save_path, self.target_path(target))
        if ensure and not os.path.isdir(path):
            os.makedirs(path, mode=0o755)
        if annotation is not None:
            path = os.path.join(path, f"{annotation}.tsv")
        return path
    def generate_figure(self, target_id, figure_name):
        """Generates a single figure for the given target and figure name."""
        target = self.config.targets[target_id]
        # Make a figure and axes for the plots.
        figsize = self.config.display.figsize
        dpi = self.config.display.dpi
        (fig,ax) = plt.subplots(1,1, figsize=figsize, dpi=dpi)
        # Run the function from the config that draws the figure.
        fn = self.config.figures[figure_name]
        meta_data = {}
        fn(target, figure_name, fig, ax, figsize, dpi, meta_data)
        # Tidy things up for image plotting.
        ax.axis('off')
        fig.subplots_adjust(0,0,1,1,0,0)
        path = self.target_figure_path(target, figure_name)
        plt.savefig(path, bbox_inches=None)
        # We also need a companion meta-data file.
        if 'xlim' not in meta_data: meta_data['xlim'] = ax.get_xlim()
        if 'ylim' not in meta_data: meta_data['ylim'] = ax.get_ylim()
        jscode = json.dumps(meta_data)
        path = os.path.join(self.target_figure_path(target), 
                            f"{figure_name}.json")
        with open(path, "wt") as f:
            f.write(jscode)
        # We can close the figure now as well.
        plt.close(fig)
    def figure(self, target_id, figure_name):
        """Returns the image and metadata for the given target and figure name.
        
        The return value is `(image_data, meta_data)` where the `image_data` is
        a numpy array of the image data, and the `meta_data` is a `dict`.
        """
        if figure_name is None:
            # This is a request for an empty image.
            return (np.zeros(self.config.display.imsize + (4,), dtype=np.uint8),
                    {'xlim':(0,1), 'ylim':(0,1)})
        impath = self.target_figure_path(target_id, figure_name)
        mdpath = os.path.join(self.target_figure_path(target_id),
                              f"{figure_name}.json")
        # If the files aren't here already, we generate them first.
        if not os.path.isfile(impath) or not os.path.isfile(mdpath):
            with self.loading_context:
                self.generate_figure(target_id, figure_name)
        # Now read them both in.
        image_data = iio.imread(impath)
        with open(mdpath, "rt") as f:
            meta_data = json.load(f)
        # And return them.
        return (image_data, meta_data)
    def generate_grid(self, target_id, annotation):
        """Generates a single figure grid for an annotation."""
        impath = self.target_grid_path(target_id, annotation)
        mdpath = os.path.join(self.target_grid_path(target_id),
                              f"{annotation}.json")
        anndata = self.config.annotations[annotation]
        grid_shape = np.shape(anndata.grid)
        # We join up the component arrays.
        figdata = [[self.figure(target_id, figname) for figname in row]
                   for row in anndata.grid]
        # Make sure the figure meta-data all match!
        md0 = figdata[0][0][1]
        for row in figdata:
            for (fig,md) in row:
                if md0['xlim'] != md['xlim']:
                    raise RuntimeError(f"not all figures have the same xlim for"
                                       f" annotation {annotation}")
                if md0['ylim'] != md['ylim']:
                    raise RuntimeError(f"not all figures have the same ylim for"
                                       f" annotation {annotation}")
        grid = np.concatenate([np.concatenate([fig for (fig,md) in row],
                                              axis=1)
                               for row in figdata],
                              axis=0)
        # Save it out as a png file.
        iio.imwrite(impath, grid)
        # And save out the meta-data.
        jscode = json.dumps(md0)
        with open(mdpath, "wt") as f:
            f.write(jscode)
    def grid(self, target_id, annotation):
        """Returns the grid of figures for the given target and annotation.

        The return value is `(image_data, grid_shape, meta_data)` where the
        `image_data` is the raw bytes of the file, `grid_shape` is a tuple of
        the `(row_count, column_count)` of the grid, and the `meta_data` is a
        `dict`.
        """
        impath = self.target_grid_path(target_id, annotation)
        mdpath = os.path.join(self.target_grid_path(target_id),
                              f"{annotation}.json")
        anndata = self.config.annotations[annotation]
        grid_shape = np.shape(anndata.grid)
        # If the files aren't here already, we generate them first.
        if not os.path.isfile(impath) or not os.path.isfile(mdpath):
            with self.loading_context:
                self.generate_grid(target_id, annotation)
        # Now read them both in.
        with open(impath, "rb") as f:
            image_data = f.read()
        with open(mdpath, "rt") as f:
            meta_data = json.load(f)
        # And return them.
        return (image_data, grid_shape, meta_data)
    def load_preferences(self):
        """Loads the preferences from the save directory and returns them.

        If no preferences file is found, an empty dictionary is returned.
        """
        path = os.path.join(self.save_path, ".annot-prefs.yaml")
        if not os.path.isfile(path):
            return {'style': {}, 'imagesize': 256}
        with open(path, "rt") as f:
            return yaml.safe_load(f)
    def save_preferences(self):
        """Saves the preferences to the save directory."""
        path = os.path.join(self.save_path, ".annot-prefs.yaml")
        with open(path, "wt") as f:
            yaml.dump(self.preferences, f)
    default_style = {"linewidth": 1,
                     "linestyle": "solid",
                     "markersize": 1,
                     "color": "black",
                     "visible": True}
    style_keys = tuple(default_style.keys())
    @classmethod
    def fix_style(cls, styledict):
        """Ensures that the given dictionary is valid as a style dictionary."""
        for (k,v) in styledict.items():
            if k not in AnnotationState.style_keys:
                raise RuntimeError("fix_style given invalid key {k}")
        # Make sure the values are also valid.
        if 'linewidth' in styledict:
            lw = styledict['linewidth']
            if lw < 0 or lw > 20:
                raise RuntimeError(f"fix_style given bad linewidth: {lw}")
        if 'linestyle' in styledict:
            ls = styledict['linestyle']
            if ls not in ('solid', 'dashed', 'dot-dashed', 'dotted'):
                raise RuntimeError(f"fix_style given bad linewidth: {lw}")
        if 'color' in styledict:
            clr = styledict['color']
            try: c = mpl.colors.to_hex(clr)
            except Exception: c = None
            if c is None:
                raise RuntimeError(f"fix_style given bad color: {c}")
            styledict['color'] = c
        if 'markersize' in styledict:
            ms = styledict['markersize']
            if ms < 0 or ms > 20:
                raise RuntimeError(f"fix_style given bad markersize: {ms}")
        if 'visible' in styledict:
            v = styledict['visible']
            if not isinstance(v, bool):
                raise RuntimeError(f"fix_style given bad visible: {v}")
        return styledict
    def style(self, annotation, *args):
        """Returns the style dict of the given annotation.

        `state.style(annot)` returns the current styledict for the
        annotation named `annot`. This style dictionary is always fully reified
        with all style keys.

        `state.style(annot, new_styledict)` updates the current styledict
        to have the contents of `new_styledict` then returns the new value.

        `state.style(annot, key, val)` is equivalent to
        `state.style(annot, {key:val})`.
        
        The styledict contains the keys `"linewidth"`, `"linestyle"`,
        `"markersize"`, `"color"`, and `"visible"`.
        """
        nargs = len(args)
        if nargs > 1 and 0 != nargs % 2:
            raise RuntimeError("invalid number of arguments given to styledict")
        # In all cases, we start by calculating our own styledict.
        # See if there is a dict in the preferences already.
        styles = self.preferences['style']
        if nargs == 0:
            # We're just returning the current reified dict.
            prefs = styles.get(annotation, {})
        elif nargs == 1:
            # We're updating the dict to have exactly these values.
            prefs = args[0]
            styles[annotation] = self.fix_style(prefs)
        else:
            update = {k:v for (k,v) in zip(args[0::2], args[1::2])}
            self.fix_style(update)
            if annotation not in styles:
                styles[annotation] = {}
            prefs = styles[annotation]
            prefs.update(update)
        # Now that we have performed the update, we just need to merge with
        # default options in order to reify the styledict.
        rval = AnnotationState.default_style.copy()
        rval.update(self.config.display.plot_options)
        if annotation is None:
            rval.update(self.config.display.fg_options)
        else:
            rval.update(self.config.annotations[annotation].plot_options)
        # Finally, merge in the user's preferences.
        rval.update(prefs)
        # And return.
        return rval
    def imagesize(self, new_imagesize=None):
        """Returns the image size from the user's preferences.

        `state.imagesize()` returns the current image size.

        `state.imagesize(new_imagesize)` updates the current image size.
        """
        if new_imagesize is None:
            return self.preferences.get('imagesize', 256)
        else:
            self.preferences['imagesize'] = new_imagesize
            return new_imagesize
    def load_target_annotation(self, tid, annot_name):
        "Loads a single annotation from the save path for a given target."
        path = self.target_save_path(tid, annot_name)
        if not os.path.isfile(path):
            # If there's no file, we return an empty matrix of points.
            return np.zeros((0,2), dtype=float)
        df = pd.read_csv(path, sep="\t", header=None)
        coords = df.values
        # The TSV file must contain an N x 2 matrix of values!
        if len(coords.shape) != 2 or coords.shape[1] != 2:
            raise RuntimeError(f"file '{path}' for annotation '{ann_name}'"
                               f" and target {tid} has invalid shape"
                               f" {coords.shape}")
        return coords
    def load_target_annotations(self, tid):
        "Loads the annotations for the current tool user for a single target"
        result = ldict()
        for name in self.config.annotations.keys():
            result[name] = delay(self.load_target_annotation, tid, name)
        return result
    def load_annotations(self):
        "Loads the annotations for the current tool user from the save path."
        return ldict({tid: delay(self.load_target_annotations, tid)
                      for tid in self.config.targets.keys()})
    def save_target_annotations(self, tid):
        "Saves the annotations for the current tool user for a single target"
        # Get the taget's annotations.
        annots = self.annotations[tid]
        for k in annots.keys():
            # Skip anything lazy. We never want to save anything that's still
            # lazy because that means that the original file hasn't been read in
            # (and thus can't have any updates).
            if annots.is_lazy(k): continue
            # Get this annotation's coordinates.
            coords = np.asarray(annots[k])
            # Make sure they're the right shape.
            if len(coords.shape) != 2 or coords.shape[1] != 2:
                raise RuntimeError(f"annotation {k} for target {tid} has"
                                   f" invalid shape {coords.shape}")
            # If they're empty, no need to save them; delete the file if it
            # exists instead.
            path = self.target_save_path(tid, k)
            if len(coords) == 0:
                if os.path.isfile(path):
                    os.remove(path)
                continue
            # Save them using pandas.
            df = pd.DataFrame(coords)
            df.to_csv(path, index=False, header=None, sep="\t")
    def save_annotations(self):
        "Saves the annotations for a given target."
        annots = self.annotations
        for tid in annots.keys():
            # Skip lazy keys; these targets have not even been loaded yet.
            if not annots.is_lazy(tid):
                self.save_target_annotations(tid)
    def save(self):
        """Saves both the user preferences and the user annotations."""
        self.save_preferences()
        self.save_annotations()
    __slots__ = ("config", "cache_path", "save_path", "git_path", "username",
                 "annotations", "preferences", "loading_context")
    def __init__(self,
                 config_path='/config/config.yaml',
                 cache_path='/cache',
                 save_path='/save',
                 git_path='/git',
                 username=None,
                 loading_context=None):
        self.config = Config(config_path)
        self.cache_path = cache_path
        self.git_path = git_path
        # We add the git username to the save path if needed here.
        if username is None:
            (git_account, git_reponame) = self.gitdata
            username = git_account
        if not isinstance(username, str):
            raise RuntimeError("username must be a string or None")
        # Build up the save path.
        self.username = username
        if username == '': self.save_path = save_path
        else:              self.save_path = os.path.join(save_path, username)
        if not os.path.isdir(self.save_path):
            os.makedirs(self.save_path, mode=0o755)
        # Use our loading control if we have one.
        if loading_context is None:
            loading_context = NoOpContext()
        self.loading_context = loading_context
        # (Lazily) load the annotations.
        self.annotations = self.load_annotations()
        # And (lazily) load the preferences.
        self.preferences = self.load_preferences()
    def apply_style(self, ann_name, canvas, style=None):
        """Applies the style associated with an annotation name to a canvas.

        `state.apply_style(name, canvas)` applies the annotation preferences
        associated with the given annotation name to the given canvas.

        Note that the annotation name `None` refers to the foreground style.

        If the requested annotation style is not visible, this function still
        applies the style but returns `False`. Otherwise, it returns `True`.

        If the optional argument `style` is given, then that style dictionary is
        used in place of the style dictionary associated with `ann_name`.
        """
        # Get the appropriate style first.
        if style is None:
            style = self.style(ann_name)
        # And walk through the key/values applying them.
        lw = style['linewidth']
        ls = style['linestyle']
        c  = style['color']
        v  = style['visible']
        canvas.line_width = lw
        if ls == 'solid':
            canvas.set_line_dash([])
        elif ls == 'dashed':
            canvas.set_line_dash([lw*3, lw*3])
        elif ls == 'dot-dashed':
            canvas.set_line_dash([lw*1, lw*2, lw*4, lw*2])
        elif ls == 'dotted':
            canvas.set_line_dash([lw, lw])
        else:
            raise RuntimeError(
                f"Invalid linestyle for annotation '{ann_name}': {ls}")
        c = mpl.colors.to_hex(c)
        canvas.stroke_style = c
        canvas.fill_style = c
        return v
    def draw_path(self, ann_name, points, canvas, path=True, style=None,
                  fixed_head=False, fixed_tail=False, cursor=None):
        """Draws the given path on the given canvas using the named style.

        `state.draw_path(name, path, canvas)` applies the style for the named
        annotation then draws the given `path` on the given `canvas`. Note that
        the `path` coordinate must be in canvas pixel coordinates, not figure
        coordinates.

        If the optional argument `path` is `False`, then only the points are
        drawn.

        If the optional argument `style` is given, then the given style dict
        is used instead of the stling for the `ann_name` annotation.
        """
        self.apply_style(ann_name, canvas, style=None)
        # First, draw stroke the path.
        if path and len(points) > 1:
            canvas.begin_path()
            (x,y) = points[0]
            canvas.move_to(x,y)
            for (x,y) in points[1:]:
                canvas.line_to(x, y)
            canvas.stroke()
        # Next, draw the points.
        sty = self.style(ann_name)
        ms = sty['markersize']
        if ms <= 0 and cursor is None: return
        if fixed_head and len(points) > 0:
            (x,y) = points[0]
            canvas.fill_rect(x-ms, y-ms, ms*2, ms*2)
            rest = points[1:]
        else:
            rest = points
        if fixed_tail and len(rest) > 0:
            (x,y) = points[-1]
            canvas.fill_rect(x-ms, y-ms, ms*2, ms*2)
            rest = points[:-1]
        else:
            rest = points
        if ms > 0:
            for (x,y) in rest:
                canvas.fill_circle(x, y, ms)
        if cursor is not None:
            ms = (ms + 1) * 4/3
            canvas.set_line_dash([])
            canvas.line_width = sty['linewidth'] * 3/4
            if len(rest) == 1:
                # We plot the circle if the cursor is head otherwise we
                # don't plot the circle.
                if cursor == 'head':
                    (x,y) = rest[0]
                else:
                    ms = 0
            elif len(rest) > 1:
                if cursor == 'head':
                    (x,y) = rest[0]
                else:
                    (x,y) = rest[-1]
            else:
                ms = 0
            if ms > 0:
                canvas.stroke_circle(x, y, ms)
        # That's all!


# The Annotation Tool ##########################################################

class AnnotationTool(ipw.HBox):
    """The core annotation tool for the `cortex-annotate` project.

    The `AnnotationTool` type handles the annotation of the cortical surface
    images for the `cortex-annotate` project.
    """
    def on_imagesize_change(self, change):
        "This method runs when the control panel's image size slider changes."
        if change.name != 'value': return
        self.state.imagesize(change.new)
        # Resize the figure panel.
        self.figure_panel.resize_canvas(change.new)
    def refresh_figure(self):
        targ = self.control_panel.target
        annot = self.control_panel.annotation
        (imdata, grid_shape, meta) = self.state.grid(targ, annot)
        im = ipw.Image(value=imdata, format='png')
        self.figure_panel.change_annotations(self.state.annotations[targ],
                                             redraw=False)
        self.figure_panel.change_foreground(self.control_panel.annotation,
                                            redraw=False)
        meta = {k:meta[k] for k in ('xlim','ylim') if k in meta}
        self.figure_panel.redraw_canvas(image=im, grid_shape=grid_shape, **meta)
    def on_selection_change(self, key, change):
        "This method runs when the control panel's selection changes."
        if change.name != 'value': return
        # First, things first: save the annotations.
        self.state.save_annotations()
        # The selection has changed; we need to redraw the image and update the
        # annotations.
        self.refresh_figure()
    def on_style_change(self, annotation, key, change):
        "This method runs when the control panel's style elements change."
        # Update the state.
        if change.name != 'value': return
        self.state.style(annotation, key, change.new)
        # Then redraw the annotation.
        self.figure_panel.redraw_canvas(redraw_image=False)
    def on_save(self, button):
        "This method runs when the control panel's save button is clicked."
        self.state.save_annotations()
        self.state.save_preferences()
    def __init__(self,
                 config_path='/config/config.yaml',
                 cache_path='/cache',
                 save_path='/save',
                 git_path='/git',
                 username=None,
                 control_panel_background_color="#f0f0f0",
                 save_button_color="#e0e0e0"):
        self.cache_path = cache_path
        self.state = AnnotationState(
            config_path=config_path,
            cache_path=cache_path,
            save_path=save_path,
            git_path=git_path,
            username=username)
        # Make the control panel.
        imagesize = self.state.imagesize()
        self.control_panel = ControlPanel(
            self.state,
            background_color=control_panel_background_color,
            save_button_color=save_button_color,
            imagesize=imagesize)
        # Make the figure panel.
        self.figure_panel = FigurePanel(
            self.state,
            imagesize=imagesize)
        # Pass the loading context over to the state.
        self.state.loading_context = self.figure_panel.loading_context
        # Go ahead and initialize the HBox component.
        super().__init__((self.control_panel, self.figure_panel))
        # Now, we want to display ourselves while we load, so do that.
        from IPython.display import display
        display(self)
        # Give the figure the initial image to plot.
        with self.state.loading_context:
            self.refresh_figure()
        # Add a listener for the image size change.
        self.control_panel.observe_imagesize(self.on_imagesize_change)
        # And a listener for the selection change.
        self.control_panel.observe_selection(self.on_selection_change)
        # And a listener for the style change.
        self.control_panel.observe_style(self.on_style_change)
        # And a listener for the save button.
        self.control_panel.observe_save(self.on_save)
        # Finally initialize the outer HBox component.

