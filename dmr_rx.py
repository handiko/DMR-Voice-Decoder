#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: DMR RX - Tier II/III decoder (RTL-SDR)
# Description: DMR Tier II/III RX, RTL-SDR, stock front end + 1 EPB burst decoder
# GNU Radio version: 3.10.10.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from PyQt5.QtCore import QObject, pyqtSlot
from gnuradio import analog
import math
from gnuradio import audio
from gnuradio import blocks
from gnuradio import eng_notation
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
import dmr_rx_dmr_dec as dmr_dec  # embedded python block
import osmosdr
import time
import sip



class dmr_rx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "DMR RX - Tier II/III decoder (RTL-SDR)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("DMR RX - Tier II/III decoder (RTL-SDR)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "dmr_rx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 960e3
        self.decim1 = decim1 = 4
        self.if_rate = if_rate = samp_rate/decim1
        self.ch_rate = ch_rate = if_rate/5
        self.sps = sps = int(round(ch_rate/4800))
        self.rrc_raw = rrc_raw = firdes.root_raised_cosine(1.0, ch_rate, 4800, 0.2, 11*sps+1)
        self.dmr_freq_entry = dmr_freq_entry = 429.325
        self.volume = volume = 1.0
        self.rrc_taps = rrc_taps = [t/sum(rrc_raw) for t in rrc_raw]
        self.rf_gain = rf_gain = 29.7
        self.ppm = ppm = 35
        self.plan_step_hz = plan_step_hz = 12500
        self.plan_ref_lpcn = plan_ref_lpcn = 151
        self.plan_ref_hz = plan_ref_hz = 148.7215e6
        self.mbelib_path = mbelib_path = ''
        self.lo_offset = lo_offset = 100e3
        self.fine_hz = fine_hz = 0
        self.dmr_freq = dmr_freq = dmr_freq_entry*1e6
        self.dev_unit = dev_unit = 648.0
        self.ch_tw = ch_tw = 2000
        self.ch_cut = ch_cut = 5000
        self.audio_slot = audio_slot = 0

        ##################################################
        # Blocks
        ##################################################

        self._rf_gain_range = qtgui.Range(0, 49.6, 0.1, 29.7, 200)
        self._rf_gain_win = qtgui.RangeWidget(self._rf_gain_range, self.set_rf_gain, "RF gain [dB]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._rf_gain_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._ppm_range = qtgui.Range(-60, 60, 0.5, 35, 200)
        self._ppm_win = qtgui.RangeWidget(self._ppm_range, self.set_ppm, "Freq corr [ppm]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._ppm_win, 0, 1, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._fine_hz_range = qtgui.Range(-30000, 30000, 100, 0, 200)
        self._fine_hz_win = qtgui.RangeWidget(self._fine_hz_range, self.set_fine_hz, "Fine tune [Hz]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._fine_hz_win, 0, 2, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        # Create the options list
        self._audio_slot_options = [0, 1, 2]
        # Create the labels list
        self._audio_slot_labels = ['Both slots', 'TS1', 'TS2']
        # Create the combo box
        self._audio_slot_tool_bar = Qt.QToolBar(self)
        self._audio_slot_tool_bar.addWidget(Qt.QLabel("Play slot" + ": "))
        self._audio_slot_combo_box = Qt.QComboBox()
        self._audio_slot_tool_bar.addWidget(self._audio_slot_combo_box)
        for _label in self._audio_slot_labels: self._audio_slot_combo_box.addItem(_label)
        self._audio_slot_callback = lambda i: Qt.QMetaObject.invokeMethod(self._audio_slot_combo_box, "setCurrentIndex", Qt.Q_ARG("int", self._audio_slot_options.index(i)))
        self._audio_slot_callback(self.audio_slot)
        self._audio_slot_combo_box.currentIndexChanged.connect(
            lambda i: self.set_audio_slot(self._audio_slot_options[i]))
        # Create the radio buttons
        self.top_grid_layout.addWidget(self._audio_slot_tool_bar, 3, 1, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.xlate_0 = filter.freq_xlating_fir_filter_ccf(decim1, firdes.low_pass(1.0, samp_rate, 40e3, 120e3), (lo_offset + fine_hz), samp_rate)
        self._volume_range = qtgui.Range(0, 8, 0.1, 1.0, 200)
        self._volume_win = qtgui.RangeWidget(self._volume_range, self.set_volume, "Volume", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._volume_win, 3, 0, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.vol = blocks.multiply_const_ff(volume)
        self.up48 = filter.rational_resampler_fff(
                interpolation=6,
                decimation=1,
                taps=[],
                fractional_bw=0)
        self.sym_filt = filter.fir_filter_fff(1, rrc_taps)
        self.sym_filt.declare_sample_delay(0)
        self.rtlsdr_source_0 = osmosdr.source(
            args="numchan=" + str(1) + " " + "rtl=0"
        )
        self.rtlsdr_source_0.set_time_unknown_pps(osmosdr.time_spec_t())
        self.rtlsdr_source_0.set_sample_rate(samp_rate)
        self.rtlsdr_source_0.set_center_freq((dmr_freq - lo_offset), 0)
        self.rtlsdr_source_0.set_freq_corr(ppm, 0)
        self.rtlsdr_source_0.set_dc_offset_mode(0, 0)
        self.rtlsdr_source_0.set_iq_balance_mode(0, 0)
        self.rtlsdr_source_0.set_gain_mode(False, 0)
        self.rtlsdr_source_0.set_gain(rf_gain, 0)
        self.rtlsdr_source_0.set_if_gain(20, 0)
        self.rtlsdr_source_0.set_bb_gain(20, 0)
        self.rtlsdr_source_0.set_antenna('', 0)
        self.rtlsdr_source_0.set_bandwidth(0, 0)
        self.quad_demod = analog.quadrature_demod_cf((ch_rate/(2*3.141592653589793*dev_unit)))
        self.hist_sym = qtgui.histogram_sink_f(
            4800,
            120,
            (-6),
            6,
            "Symbol levels: want 4 sharp peaks at -3 -1 +1 +3",
            1,
            None # parent
        )

        self.hist_sym.set_update_time(0.20)
        self.hist_sym.enable_autoscale(True)
        self.hist_sym.enable_accumulate(False)
        self.hist_sym.enable_grid(True)
        self.hist_sym.enable_axis_labels(True)

        self.hist_sym.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers= [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.hist_sym.set_line_label(i, "Data {0}".format(i))
            else:
                self.hist_sym.set_line_label(i, labels[i])
            self.hist_sym.set_line_width(i, widths[i])
            self.hist_sym.set_line_color(i, colors[i])
            self.hist_sym.set_line_style(i, styles[i])
            self.hist_sym.set_line_marker(i, markers[i])
            self.hist_sym.set_line_alpha(i, alphas[i])

        self._hist_sym_win = sip.wrapinstance(self.hist_sym.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._hist_sym_win, 2, 0, 1, 3)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.fft_if = qtgui.freq_sink_c(
            4096, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            dmr_freq, #fc
            if_rate, #bw
            "240 kHz around the channel (channel at 0)", #name
            1,
            None # parent
        )
        self.fft_if.set_update_time(0.10)
        self.fft_if.set_y_axis((-120), (-20))
        self.fft_if.set_y_label('Relative Gain', 'dB')
        self.fft_if.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.fft_if.enable_autoscale(False)
        self.fft_if.enable_grid(True)
        self.fft_if.set_fft_average(0.2)
        self.fft_if.enable_axis_labels(True)
        self.fft_if.enable_control_panel(False)
        self.fft_if.set_fft_window_normalized(False)

        self.fft_if.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.fft_if.set_line_label(i, "Data {0}".format(i))
            else:
                self.fft_if.set_line_label(i, labels[i])
            self.fft_if.set_line_width(i, widths[i])
            self.fft_if.set_line_color(i, colors[i])
            self.fft_if.set_line_alpha(i, alphas[i])

        self._fft_if_win = sip.wrapinstance(self.fft_if.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._fft_if_win, 1, 0, 1, 3)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._dmr_freq_entry_tool_bar = Qt.QToolBar(self)
        self._dmr_freq_entry_tool_bar.addWidget(Qt.QLabel("DMR Freq(MHz)" + ": "))
        self._dmr_freq_entry_line_edit = Qt.QLineEdit(str(self.dmr_freq_entry))
        self._dmr_freq_entry_tool_bar.addWidget(self._dmr_freq_entry_line_edit)
        self._dmr_freq_entry_line_edit.editingFinished.connect(
            lambda: self.set_dmr_freq_entry(eng_notation.str_to_num(str(self._dmr_freq_entry_line_edit.text()))))
        self.top_layout.addWidget(self._dmr_freq_entry_tool_bar)
        self.dmr_dec = dmr_dec.blk(sps=sps, sync_thresh=0.80, color_code=-1, dedup_s=10.0, show_data=True, invert=False, stats_every=10.0, rf_hz=dmr_freq + fine_hz, plan_ref_lpcn=plan_ref_lpcn, plan_ref_hz=plan_ref_hz, plan_step_hz=plan_step_hz, audio_slot=audio_slot, mbelib_path=mbelib_path, audio_delay=0.25)
        self.chan_lpf = filter.fir_filter_ccf(
            5,
            firdes.low_pass(
                1,
                if_rate,
                ch_cut,
                ch_tw,
                window.WIN_HAMMING,
                6.76))
        self.audio_out = audio.sink(48000, '', True)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.chan_lpf, 0), (self.quad_demod, 0))
        self.connect((self.dmr_dec, 1), (self.hist_sym, 0))
        self.connect((self.dmr_dec, 0), (self.vol, 0))
        self.connect((self.quad_demod, 0), (self.sym_filt, 0))
        self.connect((self.rtlsdr_source_0, 0), (self.xlate_0, 0))
        self.connect((self.sym_filt, 0), (self.dmr_dec, 0))
        self.connect((self.up48, 0), (self.audio_out, 0))
        self.connect((self.vol, 0), (self.up48, 0))
        self.connect((self.xlate_0, 0), (self.chan_lpf, 0))
        self.connect((self.xlate_0, 0), (self.fft_if, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "dmr_rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_if_rate(self.samp_rate/self.decim1)
        self.rtlsdr_source_0.set_sample_rate(self.samp_rate)
        self.xlate_0.set_taps(firdes.low_pass(1.0, self.samp_rate, 40e3, 120e3))

    def get_decim1(self):
        return self.decim1

    def set_decim1(self, decim1):
        self.decim1 = decim1
        self.set_if_rate(self.samp_rate/self.decim1)

    def get_if_rate(self):
        return self.if_rate

    def set_if_rate(self, if_rate):
        self.if_rate = if_rate
        self.set_ch_rate(self.if_rate/5)
        self.chan_lpf.set_taps(firdes.low_pass(1, self.if_rate, self.ch_cut, self.ch_tw, window.WIN_HAMMING, 6.76))
        self.fft_if.set_frequency_range(self.dmr_freq, self.if_rate)

    def get_ch_rate(self):
        return self.ch_rate

    def set_ch_rate(self, ch_rate):
        self.ch_rate = ch_rate
        self.set_rrc_raw(firdes.root_raised_cosine(1.0, self.ch_rate, 4800, 0.2, 11*self.sps+1))
        self.set_sps(int(round(self.ch_rate/4800)))
        self.quad_demod.set_gain((self.ch_rate/(2*3.141592653589793*self.dev_unit)))

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_rrc_raw(firdes.root_raised_cosine(1.0, self.ch_rate, 4800, 0.2, 11*self.sps+1))
        self.dmr_dec.sps = self.sps

    def get_rrc_raw(self):
        return self.rrc_raw

    def set_rrc_raw(self, rrc_raw):
        self.rrc_raw = rrc_raw
        self.set_rrc_taps([t/sum(self.rrc_raw) for t in self.rrc_raw])

    def get_dmr_freq_entry(self):
        return self.dmr_freq_entry

    def set_dmr_freq_entry(self, dmr_freq_entry):
        self.dmr_freq_entry = dmr_freq_entry
        self.set_dmr_freq(self.dmr_freq_entry*1e6)
        Qt.QMetaObject.invokeMethod(self._dmr_freq_entry_line_edit, "setText", Qt.Q_ARG("QString", eng_notation.num_to_str(self.dmr_freq_entry)))

    def get_volume(self):
        return self.volume

    def set_volume(self, volume):
        self.volume = volume
        self.vol.set_k(self.volume)

    def get_rrc_taps(self):
        return self.rrc_taps

    def set_rrc_taps(self, rrc_taps):
        self.rrc_taps = rrc_taps
        self.sym_filt.set_taps(self.rrc_taps)

    def get_rf_gain(self):
        return self.rf_gain

    def set_rf_gain(self, rf_gain):
        self.rf_gain = rf_gain
        self.rtlsdr_source_0.set_gain(self.rf_gain, 0)

    def get_ppm(self):
        return self.ppm

    def set_ppm(self, ppm):
        self.ppm = ppm
        self.rtlsdr_source_0.set_freq_corr(self.ppm, 0)

    def get_plan_step_hz(self):
        return self.plan_step_hz

    def set_plan_step_hz(self, plan_step_hz):
        self.plan_step_hz = plan_step_hz
        self.dmr_dec.plan_step_hz = self.plan_step_hz

    def get_plan_ref_lpcn(self):
        return self.plan_ref_lpcn

    def set_plan_ref_lpcn(self, plan_ref_lpcn):
        self.plan_ref_lpcn = plan_ref_lpcn
        self.dmr_dec.plan_ref_lpcn = self.plan_ref_lpcn

    def get_plan_ref_hz(self):
        return self.plan_ref_hz

    def set_plan_ref_hz(self, plan_ref_hz):
        self.plan_ref_hz = plan_ref_hz
        self.dmr_dec.plan_ref_hz = self.plan_ref_hz

    def get_mbelib_path(self):
        return self.mbelib_path

    def set_mbelib_path(self, mbelib_path):
        self.mbelib_path = mbelib_path

    def get_lo_offset(self):
        return self.lo_offset

    def set_lo_offset(self, lo_offset):
        self.lo_offset = lo_offset
        self.rtlsdr_source_0.set_center_freq((self.dmr_freq - self.lo_offset), 0)
        self.xlate_0.set_center_freq((self.lo_offset + self.fine_hz))

    def get_fine_hz(self):
        return self.fine_hz

    def set_fine_hz(self, fine_hz):
        self.fine_hz = fine_hz
        self.dmr_dec.rf_hz = self.dmr_freq + self.fine_hz
        self.xlate_0.set_center_freq((self.lo_offset + self.fine_hz))

    def get_dmr_freq(self):
        return self.dmr_freq

    def set_dmr_freq(self, dmr_freq):
        self.dmr_freq = dmr_freq
        self.dmr_dec.rf_hz = self.dmr_freq + self.fine_hz
        self.fft_if.set_frequency_range(self.dmr_freq, self.if_rate)
        self.rtlsdr_source_0.set_center_freq((self.dmr_freq - self.lo_offset), 0)

    def get_dev_unit(self):
        return self.dev_unit

    def set_dev_unit(self, dev_unit):
        self.dev_unit = dev_unit
        self.quad_demod.set_gain((self.ch_rate/(2*3.141592653589793*self.dev_unit)))

    def get_ch_tw(self):
        return self.ch_tw

    def set_ch_tw(self, ch_tw):
        self.ch_tw = ch_tw
        self.chan_lpf.set_taps(firdes.low_pass(1, self.if_rate, self.ch_cut, self.ch_tw, window.WIN_HAMMING, 6.76))

    def get_ch_cut(self):
        return self.ch_cut

    def set_ch_cut(self, ch_cut):
        self.ch_cut = ch_cut
        self.chan_lpf.set_taps(firdes.low_pass(1, self.if_rate, self.ch_cut, self.ch_tw, window.WIN_HAMMING, 6.76))

    def get_audio_slot(self):
        return self.audio_slot

    def set_audio_slot(self, audio_slot):
        self.audio_slot = audio_slot
        self._audio_slot_callback(self.audio_slot)
        self.dmr_dec.audio_slot = self.audio_slot




def main(top_block_cls=dmr_rx, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
