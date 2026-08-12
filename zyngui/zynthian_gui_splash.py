#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian GUI
#
# Zynthian GUI Splash Class
#
# Copyright (C) 2015-2026 Fernando Moyano <jofemodo@zynthian.org>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

import tkinter
from PIL import Image, ImageDraw, ImageFont
import logging
import os

# Zynthian specific modules
from zyngui import zynthian_gui_config
from zyngui.zynthian_gui_fullscreen_modal import zynthian_gui_fullscreen_modal

# ------------------------------------------------------------------------------
# Zynthian Splash GUI Class
# ------------------------------------------------------------------------------


class zynthian_gui_splash(zynthian_gui_fullscreen_modal):

    def __init__(self):
        super().__init__()
        self.zyngui = zynthian_gui_config.zyngui

        self.canvas = tkinter.Canvas(self,
                                     bg="#000000",  # Pure black to match the logo artwork's background
                                     bd=0,
                                     highlightthickness=0)
        self.canvas.grid(sticky="nsew")
        self.image = None

    def show(self, text):
        font_family = zynthian_gui_config.font_family
        # font_size is defined during config UI init; fall back to the default token
        base_font_size = getattr(zynthian_gui_config, "font_size", 16)
        if len(text) > 40:
            font_size = int(1.75 * base_font_size)
        else:
            font_size = int(2.25 * base_font_size)
        strlen = len(text) * font_size / 2
        pos_x = self.width / 2 - strlen / 2
        pos_y = int(self.height / 10)
        try:
            config_dir = os.environ.get("ZYNTHIAN_CONFIG_DIR")
            boot_file = f"{config_dir}/img/fb_zynthian_boot.jpg"
            fpath = f"{config_dir}/img/fb_zynthian_message.jpg"

            #os.system(f'convert -strip -family \\"{font_family}\\" -pointsize {font_size} -fill white -draw "text {pos_x},{pos_y} \\"{text}\\"" {config_dir}/img/fb_zynthian_boot.jpg {config_dir}/img/fb_zynthian_message.jpg')

            img = Image.open(boot_file).convert("RGB")
            draw = ImageDraw.Draw(img)
            try:
                # Brand display font, if installed
                font = ImageFont.truetype("/usr/share/fonts/truetype/Audiowide/Audiowide-Regular.ttf", font_size)
            except OSError:
                try:
                    # PIL needs a filename, not a Tk family name: "Exo 2" -> "Exo2-Regular.ttf"
                    font = ImageFont.truetype("".join(font_family.split()) + "-Regular.ttf", font_size)
                except OSError:
                    font = ImageFont.truetype("DejaVuSans.ttf", font_size)
            draw.text((pos_x, pos_y), text, fill=zynthian_gui_config.color_tx, font=font)
            img.save(fpath, "PNG")

            self.img = tkinter.PhotoImage(file=fpath)
            if self.image is None:
                self.image = self.canvas.create_image(0, 0, anchor='nw', image=self.img)
            else:
                self.canvas.itemconfig(self.image, image=self.img)
        except Exception as e:
            logging.error(e)
        super().show()

    def zynpot_cb(self, i, dval):
        pass

    def zyncoder_read(self):
        pass

    def refresh_loading(self):
        pass

    def switch_select(self, t='S'):
        pass

    def back_action(self):
        return False

# -------------------------------------------------------------------------------
