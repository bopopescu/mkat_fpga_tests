from __future__ import division

import unittest
import logging
import matplotlib
import matplotlib.pyplot as plt

import time
import itertools

import numpy as np

from unittest.util import strclass

from katcp.testutils import start_thread_with_cleanup
from corr2.dsimhost_fpga import FpgaDsimHost
from corr2.corr_rx import CorrRx

from mkat_fpga_tests import correlator_fixture
from mkat_fpga_tests.utils import normalised_magnitude, loggerise, complexise
from mkat_fpga_tests.utils import init_dsim_sources
from mkat_fpga_tests.utils import nonzero_baselines, zero_baselines, all_nonzero_baselines
from mkat_fpga_tests.utils import CorrelatorFrequencyInfo

LOGGER = logging.getLogger(__name__)

DUMP_TIMEOUT = 10              # How long to wait for a correlator dump to arrive in tests

def get_vacc_offset(xeng_raw):
    """Assuming a tone was only put into input 0, figure out if VACC is roated by 1"""
    b0 = np.abs(complexise(xeng_raw[:,0]))
    b1 = np.abs(complexise(xeng_raw[:,1]))
    if np.max(b0) > 0 and np.max(b1) == 0:
        # We expect autocorr in baseline 0 to be nonzero if the vacc is properly aligned,
        # hence no offset
        return 0
    elif np.max(b1) > 0 and np.max(b0) == 0:
        return 1
    else:
        raise ValueError('Could not determine VACC offset')

class test_CBF(unittest.TestCase):
    def setUp(self):
        self.receiver = CorrRx(port=8888)
        start_thread_with_cleanup(self, self.receiver, start_timeout=1)
        self.correlator = correlator_fixture.correlator
        self.corr_fix = correlator_fixture
        self.corr_freqs = CorrelatorFrequencyInfo(self.correlator.configd)
        dsim_conf = self.correlator.configd['dsimengine']
        dig_host = dsim_conf['host']
        self.dhost = FpgaDsimHost(dig_host, config=dsim_conf)
        self.dhost.get_system_information()
        # Increase the dump rate so tests can run faster
        self.correlator.xeng_set_acc_time(0.2)
        self.addCleanup(self.corr_fix.stop_x_data)
        self.corr_fix.start_x_data()
        self.corr_fix.issue_metadata()

    # TODO 2015-05-27 (NM) Do test using get_vacc_offset(test_dump['xeng_raw']) to see if
    # the VACC is rotated. Run this test first so that we know immediately that other
    # tests will be b0rked.

    def test_channelisation(self):
        """TP.C.1.19 CBF Channelisation Wideband Coarse L-band"""
        test_chan = 1500
        expected_fc = self.corr_freqs.chan_freqs[test_chan]

        init_dsim_sources(self.dhost)
        self.dhost.sine_sources.sin_0.set(frequency=expected_fc, scale=0.25)
        # The signal source is going to quantise the requested freqency, so see what we
        # actually got
        source_fc = self.dhost.sine_sources.sin_0.frequency

        # Get baseline 0 data, i.e. auto-corr of m000h
        test_baseline = 0
        test_data = self.receiver.get_clean_dump(DUMP_TIMEOUT)['xeng_raw']
        b_mag = normalised_magnitude(test_data[:, test_baseline, :])
        # find channel with max power
        max_chan = np.argmax(b_mag)
        # self.assertEqual(max_chan, test_chan,
        #                  'Channel with max power is not the test channel')

        requested_test_freqs = self.corr_freqs.calc_freq_samples(
            test_chan, samples_per_chan=101, chans_around=5)

        # Placeholder of actual frequencies that the signal generator produces
        actual_test_freqs = []
        # Channel magnitude responses for each frequency
        chan_responses = []
        last_source_freq = None
        for i, freq in enumerate(requested_test_freqs):
            # LOGGER.info('Getting channel response for freq {}/{}: {} MHz.'.format(
            #     i+1, len(requested_test_freqs), freq/1e6))
            print ('Getting channel response for freq {}/{}: {} MHz.'.format(
                i+1, len(requested_test_freqs), freq/1e6))
            if freq == expected_fc:
                # We've already done this one!
                this_source_freq = source_fc
                this_freq_result = b_mag
            else:
                self.dhost.sine_sources.sin_0.set(frequency=freq, scale=0.125)
                this_source_freq = self.dhost.sine_sources.sin_0.frequency
                if this_source_freq == last_source_freq:
                    LOGGER.info('Skipping channel response for freq {}/{}: {} MHz.\n'
                                'Digitiser frequency is same as previous.'.format(
                                    i+1, len(requested_test_freqs), freq/1e6))
                    continue    # Already calculated this one
                else:
                    last_source_freq = this_source_freq
                this_freq_data = self.receiver.get_clean_dump(DUMP_TIMEOUT)['xeng_raw']
                this_freq_response = normalised_magnitude(
                    this_freq_data[:, test_baseline, :])
            actual_test_freqs.append(this_source_freq)
            chan_responses.append(this_freq_response)
        self.corr_fix.stop_x_data()

        # Convert the lists to numpy arrays for easier working
        actual_test_freqs = np.array(actual_test_freqs)
        chan_responses = np.array(chan_responses)

        def plot_and_save(freqs, data, plot_filename):
            df = self.corr_freqs.delta_f
            fig = plt.plot(freqs, data)[0]
            axes = fig.get_axes()
            ybound = axes.get_ybound()
            yb_diff = abs(ybound[1] - ybound[0])
            new_ybound = [ybound[0] - yb_diff*1.1, ybound[1] + yb_diff * 1.1]
            plt.vlines(expected_fc, *new_ybound, colors='r', label='chan fc')
            plt.vlines(expected_fc - df / 2, *new_ybound, label='chan min/max')
            plt.vlines(expected_fc - 0.8*df / 2, *new_ybound, label='chan +-40%',
                       linestyles='dashed')
            plt.vlines(expected_fc + df / 2, *new_ybound, label='_chan max')
            plt.vlines(expected_fc + 0.8*df / 2, *new_ybound, label='_chan +40%',
                       linestyles='dashed')
            plt.legend()
            plt.title('Channel {} ({} MHz) response'.format(
                test_chan, expected_fc/1e6))
            axes.set_ybound(*new_ybound)
            plt.grid(True)
            plt.ylabel('dB relative to VACC max')
            # TODO Normalise plot to frequency bins
            plt.xlabel('Frequency (Hz)')
            plt.savefig(plot_filename)
            plt.close()

        graph_name = '{}.{}.channel_response.svg'.format(strclass(self.__class__),
                                                         self._testMethodName)
        plot_data_all  = loggerise(chan_responses[:, test_chan], dynamic_range=90)
        plot_and_save(actual_test_freqs, plot_data_all, graph_name)

        # Get responses for central 80% of channel
        df = self.corr_freqs.delta_f
        central_indices = (
            (actual_test_freqs <= expected_fc + 0.8*df) &
            (actual_test_freqs >= expected_fc - 0.8*df))
        central_chan_responses = chan_responses[central_indices]
        central_chan_test_freqs = actual_test_freqs[central_indices]

        # Test responses in central 80% of channel
        for i, freq in enumerate(central_chan_test_freqs):
            max_chan = np.argmax(np.abs(central_chan_responses[i]))
            self.assertEqual(max_chan, test_chan, 'Source freq {} peak not in channel '
                             '{} as expected but in {}.'
                             .format(freq, test_chan, max_chan))

        # TODO Graph the central 80%  too.
        self.assertLess(
            np.max(np.abs(central_chan_responses[:, test_chan])), 0.99,
            'VACC output at > 99% of maximum value, indicates that '
            'something, somewhere, is probably overranging.')
        max_central_chan_response = np.max(10*np.log10(central_chan_responses[:, test_chan]))
        min_central_chan_response = np.min(10*np.log10(central_chan_responses[:, test_chan]))
        chan_ripple = max_chan_response - min_chan_response
        acceptable_ripple_lt = 0.3


        self.assertLess(chan_ripple, acceptable_ripple_lt,
                        'ripple {} dB within 80% of channel fc is >= {} dB'
                        .format(chan_ripple, acceptable_ripple_lt))

        # from matplotlib import pyplot
        # colour_cycle = 'rgbyk'
        # style_cycle = ['-', '--']
        # linestyles = itertools.cycle(itertools.product(style_cycle, colour_cycle))
        # for i, freq in enumerate(actual_test_freqs):
        #     style, colour = linestyles.next()
        #     pyplot.plot(loggerise(chan_responses[:, i], dynamic_range=60), color=colour, ls=style)
        # pyplot.ion()
        # pyplot.show()
        # import IPython ; IPython.embed()

    def test_product_baselines(self):
        """CBF Baseline Correlation Products: VR.C.19, TP.C.1.3"""

        init_dsim_sources(self.dhost)
        # Put some correlated noise on both outputs
        self.dhost.noise_sources.noise_corr.set(scale=0.5)
        test_dump = self.receiver.get_clean_dump(DUMP_TIMEOUT)

        # Get list of all the correlator input labels
        input_labels = sorted(tuple(test_dump['input_labelling'][:,0]))
        # Get list of all the baselines present in the correlator output
        present_baselines = sorted(
            set(tuple(bl) for bl in test_dump['bls_ordering']))

        # Make a list of all possible baselines (including redundant baselines) for the
        # given list of inputs
        possible_baselines = set()
        for li in input_labels:
            for lj in input_labels:
                possible_baselines.add((li, lj))

        test_bl = sorted(list(possible_baselines))
        # Test that each baseline (or its reverse-order counterpart) is present in the
        # correlator output
        baseline_is_present = {}

        for test_bl in possible_baselines:
           baseline_is_present[test_bl] = (test_bl in present_baselines or
                                           test_bl[::-1] in present_baselines)
        self.assertTrue(all(baseline_is_present.values()),
                        "Not all baselines are present in correlator output.")

        test_data = test_dump['xeng_raw']
        # Expect all baselines and all channels to be non-zero
        self.assertFalse(zero_baselines(test_data))
        self.assertEqual(nonzero_baselines(test_data),
                         all_nonzero_baselines(test_data))

        # Save initial f-engine equalisations
        initial_equalisations = {input: eq_info['eq'] for input, eq_info
                                in self.correlator.feng_eq_get().items()}
        def restore_initial_equalisations():
            for input, eq in initial_equalisations.items():
                self.correlator.feng_eq_set(source_name=input, new_eq=eq)
        self.addCleanup(restore_initial_equalisations)

        # Set all inputs to zero, and check that output product is all-zero
        for input in input_labels:
            self.correlator.feng_eq_set(source_name=input, new_eq=0)
        test_data = self.receiver.get_clean_dump(DUMP_TIMEOUT)['xeng_raw']
        self.assertFalse(nonzero_baselines(test_data))
        #-----------------------------------
        all_inputs = sorted(set(input_labels))
        zero_inputs = set(input_labels)
        nonzero_inputs = set()

        def calc_zero_and_nonzero_baselines(nonzero_inputs):
            nonzeros = set()
            zeros = set()
            for inp_i in all_inputs:
                for inp_j in all_inputs:
                    if inp_i in nonzero_inputs and inp_j in nonzero_inputs:
                        nonzeros.add((inp_i, inp_j))
                    else:
                        zeros.add((inp_i, inp_j))
            return zeros, nonzeros

        #zero_baseline, nonzero_baseline = calc_zero_and_nonzero_baselines(nonzero_inputs)
        def print_baselines():
            print ('zeros: {}\n\nnonzeros: {}\n\nnonzero-baselines: {}\n\n '
                'zero-baselines: {}\n\n'.format(
                    sorted(zero_inputs), sorted(nonzero_inputs),
                    sorted(nonzero_baseline), sorted(zero_baseline)))
        #print_baselines()
        for inp in input_labels:
            old_eqs = initial_equalisations[inp]
            self.correlator.feng_eq_set(source_name=inp, new_eq=old_eqs)
            zero_inputs.remove(inp)
            nonzero_inputs.add(inp)
            #zero_baseline, nonzero_baseline = calc_zero_and_nonzero_baselines(nonzero_inputs)
            #print_baselines()
        #print self.correlator.feng_eq_get().items()