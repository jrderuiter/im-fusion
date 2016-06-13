# pylint: disable=W0622,W0614,W0401
from __future__ import absolute_import, division, print_function
from builtins import *
# pylint: enable=W0622,W0614,W0401

import logging
import os
import sys
import subprocess

try:
    # Python 3.0+
    from subprocess import DEVNULL
except ImportError:
    # Python 2.x
    DEVNULL = open(os.devnull, 'wb')

try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path

import pandas as pd

from imfusion.model import Fusion

from imfusion.util.check import check_features
from imfusion.util.fusions import annotate_fusions, place_fusions
from imfusion.util.insertions import filter_invalid_insertions
from imfusion.util.shell import format_kwargs
from imfusion.util.tabix import GtfFile


def identify_insertions(fastqs, index_path, reference_gtf_path,
                        transposon_name, transposon_features, sample_id,
                        work_dir, min_flank, tophat_kws=None,
                        transcriptome_index=None):
    """Identifies insertions from RNA-seq fusions using Tophat2.

    Main function for identifying fusions from RNA-seq fastq files using Tophat2. The function essentially consists of four main steps:

        - The identification of gene-transposon fusions using Tophat2
        - Annotation of the found fusions for gene/transposon features
        - Deriving approximate locations for the corresponding insertions.
        - Filtering of fusions that are biologically implausible (for
          example due to their relative orientation)

    The function returns the list of insertions that were identified by
    Tophat2. The generated alignment is also symlinked into the work directory
    as 'alignment.bam' for convenient access.

    Parameters
    ----------
    fastqs : list[pathlib.Path] or list[tuple(pathlib.Path, pathlib.Path)]
        Paths to the fastq files that should be used for the Tophat2
        alignment. Can be given as a list of file paths for single-end
        sequencing data, or a list of path tuples for paired-end sequencing
        data. The fastqs are treated as belonging to a single sample.
    index_path : pathlib.Path
        Path to the bowtie index of the (augmented)
        genome that should be used in the alignment. This index is
        typically generated by the *build_reference* function.
    reference_gtf_path : pathlib.Path:
        Path to the gtf file containing genomic
        features. This file is used by Tophat2 for known gene features and
        for the annotation of gene features for identified fusions.
    transposon_name : str
        Name of the transposon sequence in the augmented reference genome.
    transposon_features :pandas.DataFrame
        Dataframe containing positions
        for the features present in the transposon sequence. Used to
        identify transposon features (such as splice acceptors or donors)
        that are involed in the identified fusions.
    sample_id : str
        Sample name that the identified insertions should be assigned to.
    work_dir : pathlib.Path
        Path to the working directory.
    min_flank : int
        Minimum amount of flanking region that should be
        surrounding the fusion. Used by Tophat2 in its identification
        of fusions during the alignment.
    tophat_kws : dict
        Dict of extra arguments for Tophat2.

    Yields
    -------
    Insertion
            Next insertion that was identified in the given sample.

    """

    # Sanity checks for inputs.
    # TODO: check GTF?
    check_features(transposon_features)

    # Identify fusions.
    fusions = identify_fusions(
        fastqs,
        index_path=index_path,
        transposon_name=transposon_name,
        reference_gtf_path=reference_gtf_path,
        work_dir=work_dir,
        min_flank=min_flank,
        tophat_kws=tophat_kws,
        transcriptome_index=transcriptome_index)

    # Annotate fusions for gene/transposon features and strands.
    reference_gtf = GtfFile(reference_gtf_path)

    logging.info('-- Annotating gene-transposon fusions')
    annotated_fusions = annotate_fusions(
        fusions, reference_gtf, transposon_features)

    # Filter insertions without or with an unknown transposon feature.
    filtered_fusions = (fusion for fusion in annotated_fusions
                        if fusion.feature_type in {'SD', 'SA'})

    # Determine approximate genomic position for insertions.
    logging.info('-- Converting to insertions')
    insertions = place_fusions(filtered_fusions, sample_id, reference_gtf)

    # Filter wrong insertions.
    insertions = filter_invalid_insertions(insertions)

    return insertions


def identify_fusions(fastqs, index_path, reference_gtf_path, transposon_name,
                     work_dir, min_flank=20, use_existing=True,
                     tophat_kws=None, transcriptome_index=None):
    """Identifies gene-transposon fusions from RNA-seq data using Tophat2.

    fastqs : list[pathlib.Path] or list[tuple(pathlib.Path, pathlib.Path)]
        Paths to the fastq files that should be used for the Tophat2
        alignment. Can be given as a list of file paths for single-end
        sequencing data, or a list of path tuples for paired-end sequencing
        data. The fastqs are treated as belonging to a single sample.
    index_path : pathlib.Path)
        Path to the bowtie index of the (augmented)
        genome that should be used in the alignment. This index is
        typically generated by the *build_reference* function.
    reference_gtf_path : pathlib.Path:
        Path to the gtf file containing genomic
        features. This file is used by Tophat2 for known gene features and
        for the annotation of gene features for identified fusions.
    transposon_name : str
        Name of the transposon sequence in the augmented reference genome.
    work_dir : pathlib.Path
        Path to the working directory.
    min_flank : int
        Minimum amount of flanking region that should be
        surrounding the fusion. Used by Tophat2 in its identification
        of fusions during the alignment.
    use_existing : bool
        Whether to use existing Tophat2 output if present (default = True).
    tophat_kws : dict
        Dict of extra arguments for Tophat2.
    transcriptome_index : pathlib.Path
        Path to the transcriptome index to use in the alignment. Only needed
        if different from the default path used by `build_reference`.

    Yields
    -------
    Fusion
        Next fusion that was identified in the given sample.

    """

    tophat_kws = tophat_kws or {}

    if transcriptome_index is None:
        # Check if index exists at default location.
        tr_base = index_path.with_suffix(index_path.suffix + '.transcriptome')
        if tr_base.with_suffix(tr_base.suffix + '.1.ebwt').exists():
            transcriptome_index = tr_base

    # Use sub-directory as work_dir for tophat.
    tophat_dir = work_dir / 'tophat'

    # Expected path for fusion file.
    fusion_path = tophat_dir / 'fusions.out'

    if not use_existing or not fusion_path.exists():
        logging.info('-- Running alignment')
        # Perform alignment to identify fusions.

        # Add our arguments to any passed kwargs.
        tophat_kws.update({'--fusion-search': True,
                           '--fusion-anchor-length': min_flank,
                           '--bowtie1': True})

        # Inject index or gtf if given.
        if transcriptome_index is not None:
            tophat_kws['--transcriptome-index'] = str(transcriptome_index)
        elif reference_gtf_path is not None:
            tophat_kws.pop('-G', None)
            tophat_kws['--GTF'] = str(reference_gtf_path)

        # Do alignment with Tophat2.
        tophat2_align(fastqs=fastqs, output_dir=tophat_dir,
                      index_path=index_path, kwargs=tophat_kws)
    else:
        logging.warning('-- Using existing tophat alignment')

    # Symlink alignment.
    aln_src_path = tophat_dir / 'accepted_hits.bam'
    aln_tgt_path = work_dir / 'alignment.bam'

    if aln_tgt_path.exists():
        aln_tgt_path.unlink()

    aln_tgt_path.symlink_to(aln_src_path.relative_to(aln_tgt_path.parent))

    # Extract fusions.
    fusions = extract_fusions(fusion_path, transposon_name)

    return fusions


def _has_index(base_path):
    """Returns True if index exists."""
    return Path(str(base_path) + '.1.ebwt').exists()


def extract_fusions(fusion_path, transposon_name):
    """Extracts gene-transposon fusions from a Tophat fusion.out file.

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

    # Read fusions and extract insertions.
    fusions = read_fusions(fusion_path)

    # Select fusions where one seqname is the transposon and the other isn't.
    fusions = fusions.ix[((fusions['seqname_a'] == transposon_name) ^
                          (fusions['seqname_b'] == transposon_name))]

    # Build frame with candidate insertions.
    for _, fusion_row in fusions.iterrows():
        yield _to_fusion_obj(fusion_row, transposon_name)


def _to_fusion_obj(fusion, transposon_name):
    if fusion.seqname_a == transposon_name:
        gen_id, tr_id = 'b', 'a'
    else:
        gen_id, tr_id = 'a', 'b'

    return Fusion(seqname=fusion['seqname_' + gen_id],
                  anchor_genome=fusion['location_' + gen_id],
                  anchor_transposon=fusion['location_' + tr_id],
                  flank_genome=fusion['flank_' + gen_id],
                  flank_transposon=fusion['flank_' + tr_id],
                  strand_genome=fusion['strand_' + gen_id],
                  strand_transposon=fusion['strand_' + tr_id],
                  spanning_reads=fusion.supp_reads,
                  supporting_mates=fusion.supp_mates,
                  spanning_mates=fusion.supp_spanning_mates)


def read_fusions(file_path):
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

    # Read fusions using pandas.
    col_names = ['seqnames', 'location_a', 'location_b', 'orientation',
                 'supp_reads', 'supp_mates', 'supp_spanning_mates',
                 'contradicting_reads', 'flank_a', 'flank_b']

    fusions = pd.read_csv(str(file_path), sep='\t', header=None,
                          usecols=range(0, 10), names=col_names)

    # Split combined entries.
    fusions = _split_fields(fusions)

    # Map orientation to strands.
    fusions['strand_a'] = fusions['strand_a'].map({'f': 1, 'r': -1})
    fusions['strand_b'] = fusions['strand_b'].map({'f': 1, 'r': -1})

    return fusions


def _split_fields(fusions):
    """Splits combined seqnames/strand entries in tophat fusion frame."""

    columns = ['seqname_a', 'location_a', 'strand_a',
               'seqname_b', 'location_b', 'strand_b',
               'supp_reads', 'supp_mates', 'supp_spanning_mates',
               'contradicting_reads', 'flank_a', 'flank_b']

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


def tophat2_align(fastqs, index_path, output_dir, kwargs=None,
                  path=None, log_path=None, check_python=True):
    """Aligns fastq files to a reference genome using TopHat2.

    This function is used to call TopHat2 from Python to perform an
    RNA-seq alignment. As Tophat2 is written in Python 2.7, this function
    cannot be used in Python 3.0+.

    Parameters
    ----------
    fastqs : list[pathlib.Path] or list[tuple(pathlib.Path, pathlib.Path)]
        Paths to the fastq files that should be used for the Tophat2
        alignment. Can be given as a list of file paths for single-end
        sequencing data, or a list of path tuples for paired-end sequencing
        data. The fastqs are treated as belonging to a single sample.
    index_path : pathlib.Path)
        Path to the bowtie index of the (augmented)
        genome that should be used in the alignment. This index is
        typically generated by the *build_reference* function.
    output_dir : pathlib.Path
            Path to the output directory.
    kwargs : dict
        Dict of extra command line arguments for Tophat2.
    path : pathlib.Path
        Path to the Tophat2 executable.
    check_python (bool): Whether to check if we are running on Python 2.
        Raises ValueError if this is not the case and check_python is True.

    Returns
    -------
    str
        Path to output directory (containing the alignment).

    """

    tophat_kwargs = kwargs or dict()

    # Check Python version.
    if check_python and sys.version_info >= (3, ):
        raise ValueError('Python 3.x is not supported for identifying '
                         'insertions as TopHat2 does not support Python 3')

    # Create output_dir if needed.
    if not output_dir.exists():
        output_dir.mkdir(parents=True)

    # Extract fastq paths and concatenate into single str.
    if isinstance(fastqs[0], tuple):
        fastqs_1, fastqs_2 = zip(*fastqs)
    else:
        fastqs_1, fastqs_2 = fastqs, None

    # Concatenate inputs.
    fastqs_1 = ','.join(str(fp) for fp in  fastqs_1)
    fastqs_2 = ','.join((str(fp) for fp in fastqs_2)) \
        if fastqs_2 is not None else None
    
    # Remove output_dir kwargs, as we override these with our
    # own arguments anyway. After removing, inject out output_dir.
    overridden_kwargs = {'-o', '--output-dir'}
    tophat_kwargs = {k: v for k, v in tophat_kwargs.items()
                     if k not in set(overridden_kwargs)}

    tophat_kwargs['--output-dir'] = str(output_dir)

    # Build command-line arguments.
    tophat2_path = Path(path or '') / 'tophat2'
    optional_args = list(format_kwargs(tophat_kwargs))

    positional_args = [str(index_path), fastqs_1]
    if fastqs_2 is not None:
        positional_args.append(fastqs_2)

    cmdline_args = [str(tophat2_path)] + optional_args + positional_args

    # Run Tophat2!
    if log_path is None:
        subprocess.check_call(args=cmdline_args, stderr=DEVNULL)
    else:
        with log_path.open('w') as log_file:
            subprocess.check_output(args=cmdline_args, stderr=log_file)

    return output_dir
