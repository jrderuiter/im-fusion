# -*- coding: utf-8 -*-
"""Implements aligner for building Tophat2 references."""

# pylint: disable=wildcard-import,redefined-builtin,unused-wildcard-import
from __future__ import absolute_import, division, print_function
from builtins import *
# pylint: enable=wildcard-import,redefined-builtin,unused-wildcard-import

try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path

import pandas as pd
import toolz

from imfusion.build.indexers.tophat import TophatReference
from imfusion.model import TransposonFusion
from imfusion.util import shell, path, tabix
from imfusion.util.frozendict import frozendict

from .base import Aligner, register_aligner
from .. import util


class TophatAligner(Aligner):
    """Tophat2 aligner."""

    def __init__(self,
                 reference,
                 assemble=False,
                 assemble_args=None,
                 min_flank=12,
                 threads=1,
                 extra_args=None,
                 logger=None,
                 filter_features=True,
                 filter_orientation=True,
                 filter_blacklist=None):

        super().__init__(reference=reference, logger=logger)

        self._assemble = assemble
        self._assemble_args = assemble_args or {}
        self._min_flank = min_flank
        self._threads = threads
        self._extra_args = extra_args or {}

        self._filter_features = filter_features
        self._filter_orientation = filter_orientation
        self._filter_blacklist = filter_blacklist

    @property
    def dependencies(self):
        programs = ['tophat2', 'bowtie']

        if self._assemble:
            programs += ['stringtie']

        return programs

    def identify_insertions(self, fastq_path, output_dir, fastq2_path=None):
        """Identifies insertions from given reads."""

        # Perform alignment using STAR.
        alignment_path = output_dir / 'alignment.bam'
        if not alignment_path.exists():
            self._logger.info('Performing alignment')
            self._align(fastq_path, output_dir, fastq2_path=fastq2_path)
        else:
            self._logger.info('Using existing alignment')

        # Assemble transcripts if requested.
        if self._assemble:
            assembled_path = output_dir / 'assembled.gtf.gz'
            if not assembled_path.exists():
                self._logger.info('Assembling transcripts')

                # Generate assembled GTF.
                stringtie_out_path = assembled_path.with_suffix('')
                util.stringtie_assemble(
                    alignment_path,
                    gtf_path=self._reference.gtf_path,
                    output_path=stringtie_out_path)

                # Compress and index.
                tabix.index_gtf(stringtie_out_path, output_path=assembled_path)
                stringtie_out_path.unlink()
            else:
                self._logger.info('Using existing assembly')
        else:
            assembled_path = None

        # Extract identified fusions.
        self._logger.info('Extracting fusions')
        fusion_path = output_dir / 'fusions.out'
        fusions = self._extract_fusions(fusion_path)

        # Extract insertions.
        self._logger.info('Extracting insertions')
        insertions = list(
            util.extract_insertions(
                fusions,
                gtf_path=self._reference.indexed_gtf_path,
                features_path=self._reference.features_path,
                assembled_gtf_path=assembled_path,
                ffpm_fastq_path=fastq_path,
                chromosomes=None))

        self._logger.info('Filtering insertions')
        insertions = util.filter_insertions(
            insertions,
            features=self._filter_features,
            orientation=self._filter_orientation,
            blacklist=self._filter_blacklist)

        for insertion in insertions:
            yield insertion

    def _align(self, fastq_path, output_dir, fastq2_path=None):
        # Setup args.
        transcriptome_path = self._reference.transcriptome_path

        args = {
            '--transcriptome-index': (str(transcriptome_path), ),
            '--fusion-search': (),
            '--fusion-anchor-length': (self._min_flank, ),
            '--bowtie1': (),
            '--num-threads': (self._threads, )
        }

        args = toolz.merge(args, self._extra_args)

        # Run Tophat2.
        tophat_dir = output_dir / '_tophat'

        tophat2_align(
            fastq_path=fastq_path,
            fastq2_path=fastq2_path,
            index_path=self._reference.index_path,
            output_dir=tophat_dir,
            extra_args=args,
            logger=self._logger)

        # Symlink alignment into expected location for gene counts.
        path.symlink_relative(
            src_path=tophat_dir / 'accepted_hits.bam',
            dest_path=output_dir / 'alignment.bam')

        path.symlink_relative(
            src_path=tophat_dir / 'fusions.out',
            dest_path=output_dir / 'fusions.out')

    def _extract_fusions(self, fusion_path):
        fusion_data = read_fusion_out(fusion_path)

        fusions = extract_transposon_fusions(
            fusion_data, transposon_name=self._reference.transposon_name)

        for fusion in fusions:
            yield fusion

    @classmethod
    def configure_args(cls, parser):
        super().configure_args(parser)

        group = parser.add_argument_group('Tophat2 arguments')
        group.add_argument('--tophat_threads', type=int, default=1)
        group.add_argument('--tophat_min_flank', type=int, default=12)
        group.add_argument(
            '--tophat_args', type=shell.parse_arguments, default='')

        assemble_group = parser.add_argument_group('Assembly')
        assemble_group.add_argument(
            '--assemble', default=False, action='store_true')

        filt_group = parser.add_argument_group('Filtering')
        filt_group.add_argument(
            '--no_filter_orientation',
            dest='filter_orientation',
            default=True,
            action='store_false')
        filt_group.add_argument(
            '--no_filter_feature',
            dest='filter_features',
            default=True,
            action='store_false')
        filt_group.add_argument('--blacklisted_genes', nargs='+')

    @classmethod
    def parse_args(cls, args):
        return dict(
            reference=TophatReference(args.reference),
            min_flank=args.tophat_min_flank,
            threads=args.tophat_threads,
            extra_args=args.tophat_args,
            assemble=args.assemble,
            filter_features=args.filter_features,
            filter_orientation=args.filter_orientation,
            filter_blacklist=args.blacklisted_genes)


register_aligner('tophat', TophatAligner)


def tophat2_align(fastq_path,
                  index_path,
                  output_dir,
                  fastq2_path=None,
                  extra_args=None,
                  stdout=None,
                  stderr=None,
                  logger=None):
    """Aligns fastq files to a reference genome using Tophat2.

    This function is used to call TopHat2 from Python to perform an
    RNA-seq alignment. As Tophat2 is written in Python 2.7, this function
    cannot be used in Python 3.0+.

    Parameters
    ----------
    fastq_path : pathlib.Path
        Paths to the fastq file that should be used for the Tophat2
        alignment.
    index_path : pathlib.Path
        Path to the bowtie index of the (augmented)
        genome that should be used in the alignment. This index is
        typically generated by the *build_reference* function.
    output_dir : pathlib.Path
        Path to the output directory.
    fastq2_path : pathlib.Path
        Path to the fastq file of the second pair (for paired-end sequencing).
    kwargs : dict
        Dict of extra command line arguments for Tophat2.
    path : pathlib.Path
        Path to the Tophat2 executable.

    """

    extra_args = extra_args or {}

    # Create output_dir if needed.
    if not output_dir.exists():
        output_dir.mkdir(parents=True)

    # Inject own arguments.
    extra_args['--output-dir'] = (str(output_dir), )

    # Build command-line arguments.
    if fastq2_path is None:
        fastqs = [str(fastq_path)]
    else:
        fastqs = [str(fastq_path), str(fastq2_path)]

    optional_args = list(shell.flatten_arguments(extra_args))
    positional_args = [str(index_path)] + fastqs

    cmdline_args = ['tophat2'] + optional_args + positional_args
    cmdline_args = [str(s) for s in cmdline_args]

    # Run Tophat2!
    shell.run_command(
        args=cmdline_args, stdout=stdout, stderr=stderr, logger=logger)


def read_fusion_out(fusion_path):
    """Reads fusion.out file from Tophat2.

    Parameters
    ----------
    file_path : pathlib.Path
        Path to the Tophat fusion file (fusions.out).

    Returns
    -------
    pandas.DataFrame
        DataFrame containing gene fusions.

    """

    if isinstance(fusion_path, Path):
        fusion_path = str(fusion_path)

    # Read fusions using pandas.
    names = [
        'seqnames', 'location_a', 'location_b', 'orientation', 'supp_reads',
        'supp_mates', 'supp_spanning_mates', 'contradicting_reads', 'flank_a',
        'flank_b'
    ]

    fusions = pd.read_csv(
        fusion_path, sep='\t', header=None, usecols=range(0, 10), names=names)

    # Split combined entries.
    fusions = _split_fields(fusions)

    # Map orientation to strands.
    fusions['strand_a'] = fusions['strand_a'].map({'f': 1, 'r': -1})
    fusions['strand_b'] = fusions['strand_b'].map({'f': 1, 'r': -1})

    return fusions


def _split_fields(fusions):
    """Splits combined seqnames/strand entries in tophat fusion frame."""

    columns = [
        'seqname_a', 'location_a', 'strand_a', 'seqname_b', 'location_b',
        'strand_b', 'supp_reads', 'supp_mates', 'supp_spanning_mates',
        'contradicting_reads', 'flank_a', 'flank_b'
    ]

    if len(fusions) == 0:
        # Return empty 'split' frame.
        fusions = pd.DataFrame.from_records([], columns=columns)
    else:
        # Split combined entries.
        fusions['seqname_a'], fusions['seqname_b'] = \
            zip(*fusions['seqnames'].str.split('-'))

        fusions['strand_a'], fusions['strand_b'] = \
            zip(*fusions['orientation'].apply(list))

        # Subset/Reorder columns.
        fusions = fusions[columns]

    return fusions


def extract_transposon_fusions(fusion_data, transposon_name):
    """
    Extracts gene-transposon fusions from a Tophat fusion.out file.

    Parameters
    ----------
    file_path : pathlib.Path
        Path to the Tophat fusion file (fusions.out).
    transposon_name : str
        Name of the transposon sequence in the augmented reference
        genome that was used for the alignment.

    Returns
    -------
    pandas.DataFrame
        DataFrame containing gene-transposon fusions.

    """

    is_paired = (any(fusion_data['supp_mates'] > 0) or
                 any(fusion_data['supp_spanning_mates'] > 0))

    # Select fusions where one seqname is the transposon and the other isn't.
    fusion_data = fusion_data.ix[(
        (fusion_data['seqname_a'] == transposon_name) ^
        (fusion_data['seqname_b'] == transposon_name))]

    # Build frame with candidate insertions.
    for _, fusion_row in fusion_data.iterrows():
        yield _to_fusion_obj(fusion_row, transposon_name, is_paired)


def _to_fusion_obj(fusion, transposon_name, is_paired):
    if fusion.seqname_a == transposon_name:
        gen_id, tr_id = 'b', 'a'
        gen_dir, tr_dir = 1, -1
    else:
        gen_id, tr_id = 'a', 'b'
        gen_dir, tr_dir = -1, 1

    strand_genome = fusion['strand_' + gen_id]
    strand_transposon = fusion['strand_' + tr_id]

    if is_paired:
        support_junction = fusion.supp_spanning_mates
        support_spanning = fusion.supp_mates
    else:
        support_junction = fusion.supp_reads
        support_spanning = 0

    return TransposonFusion(
        seqname=fusion['seqname_' + gen_id],
        anchor_genome=fusion['location_' + gen_id],
        anchor_transposon=fusion['location_' + tr_id],
        flank_genome=fusion['flank_' + gen_id] * strand_genome * gen_dir,
        flank_transposon=fusion['flank_' + tr_id] * strand_transposon * tr_dir,
        strand_genome=strand_genome,
        strand_transposon=strand_transposon,
        support_junction=support_junction,
        support_spanning=support_spanning,
        metadata=frozendict())
