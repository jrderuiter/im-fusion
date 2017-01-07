# pylint: disable=W0622,W0614,W0401
from __future__ import absolute_import, division, print_function
from builtins import *
# pylint: enable=W0622,W0614,W0401

import logging
import shutil

try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path

import pyfaidx

from imfusion.util import shell, tabix

from .. import util as build_util

_indexer_registry = {}


def register_indexer(name, indexer):
    """Registers indexer under given name."""
    _indexer_registry[name] = indexer


def get_indexers():
    """Returns dict of available indexers."""
    return dict(_indexer_registry)


class Indexer(object):
    """Base Indexer class."""

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger()

    @property
    def _reference_class(self):
        """Reference class to use for this indexer."""
        return Reference

    @property
    def dependencies(self):
        """External dependencies required by Indexer."""
        return []

    def check_dependencies(self):
        """Checks if all required external dependencies are available."""
        shell.check_dependencies(self.dependencies)

    def build(self,
              refseq_path,
              gtf_path,
              transposon_path,
              transposon_features_path,
              output_dir,
              blacklist_regions=None,
              blacklist_genes=None):
        """Builds an indexed reference containing the transposon sequence."""

        # Create output directory.
        output_dir.mkdir(parents=True, exist_ok=False)

        # Use dummy Reference instance for paths.
        reference = self._reference_class(output_dir)

        # Copy and index additional files (GTF etc.).
        self._copy_and_index_files(
            reference=reference,
            gtf_path=gtf_path,
            transposon_path=transposon_path,
            transposon_features_path=transposon_features_path)

        # Build augmented reference.
        self._build_reference(
            reference=reference,
            refseq_path=refseq_path,
            transposon_path=transposon_path,
            blacklist_genes=blacklist_genes,
            blacklist_regions=blacklist_regions)

        # Build any required indices using files.
        self._build_indices(reference)

    def _build_reference(self,
                         reference,
                         refseq_path,
                         transposon_path,
                         blacklist_regions=None,
                         blacklist_genes=None):

        self._logger.info('Building augmented reference')

        blacklist_regions = blacklist_regions or []
        blacklist_genes = blacklist_genes or []

        blacklist = (build_util.regions_from_strings(blacklist_regions) +
                     build_util.regions_from_genes(blacklist_genes,
                                                   reference.indexed_gtf_path))

        build_util.build_reference(
            refseq_path,
            transposon_path,
            output_path=reference.fasta_path,
            blacklisted_regions=blacklist)

    def _copy_and_index_files(self, reference, gtf_path, transposon_path,
                              transposon_features_path):
        """Copies and indexes additional reference files (GTF, transposon)."""

        # Copy additional reference files.
        self._logger.info('Copying files')
        shutil.copy(str(transposon_path), str(reference.transposon_path))

        shutil.copy(str(transposon_features_path),
                    str(reference.features_path)) # yapf: disable

        shutil.copy(str(gtf_path), str(reference.gtf_path))

        self._logger.info('Indexing reference gtf')
        tabix.index_gtf(reference.gtf_path,
                        output_path=reference.indexed_gtf_path) # yapf: disable

    def _build_indices(self, reference):
        raise NotImplementedError()

    @classmethod
    def configure_args(cls, parser):
        """Configures an argument parser for the Indexer."""

        # Basic arguments.
        base_group = parser.add_argument_group('Basic arguments')

        base_group.add_argument(
            '--reference_seq',
            type=Path,
            required=True,
            help='Path to the reference sequence (in Fasta format).')

        base_group.add_argument(
            '--reference_gtf',
            type=Path,
            required=True,
            help='Path to the reference gtf file.')

        base_group.add_argument(
            '--transposon_seq',
            type=Path,
            required=True,
            help='Path to the transposon sequence (in Fasta format).')

        base_group.add_argument(
            '--transposon_features',
            type=Path,
            required=True,
            help='Path to the transposon features (tsv).')

        base_group.add_argument('--output_dir', type=Path, required=True)

        # Optional blacklist arguments.
        blacklist_group = parser.add_argument_group('Blacklist arguments')

        blacklist_group.add_argument(
            '--blacklist_regions',
            nargs='+',
            default=(),
            help='Regions of the reference to blacklist. Should '
            'be specified as \'chromosome:start-end\'.')

        blacklist_group.add_argument(
            '--blacklist_genes',
            nargs='+',
            default=(),
            help='Genes to blacklist. Should correspond with '
            'the gene ids used in the reference gtf file.')

    @classmethod
    def parse_args(cls, args):
        """Parses argparse argument to a dict."""
        return {}

    @classmethod
    def from_args(cls, args):
        """Constructs an Indexer instance from given arguments."""
        return cls(**cls.parse_args(args))


class Reference(object):
    """Reference class."""

    def __init__(self, reference_path):
        if not reference_path.exists():
            raise ValueError('Reference path does not exist')
        self._reference = reference_path

    @property
    def base_path(self):
        """Path to reference base directory."""
        return self._reference

    @property
    def fasta_path(self):
        """Path to reference sequence."""
        return self._reference / 'reference.fa'

    @property
    def gtf_path(self):
        """Path to reference gtf."""
        return self._reference / 'reference.gtf'

    @property
    def indexed_gtf_path(self):
        """Path to reference gtf."""
        return self._reference / 'reference.gtf.gz'

    @property
    def index_path(self):
        """Path to index."""
        return self._reference / 'index'

    @property
    def transposon_name(self):
        """Name of transposon sequence."""
        seqs = pyfaidx.Fasta(str(self.transposon_path)).keys()
        return list(seqs)[0]

    @property
    def transposon_path(self):
        """Name of transposon sequence."""
        return self._reference / 'transposon.fa'

    @property
    def features_path(self):
        """Path to transposon features."""
        return self._reference / 'features.txt'
