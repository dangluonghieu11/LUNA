from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Align import MultipleSeqAlignment
from Bio.Alphabet import generic_protein

from file.validator import (is_directory_valid, is_file_valid)
from util.exceptions import InvalidSuperpositionFileError

import math
import os.path
import subprocess

import logging
logger = logging.getLogger(__name__)

TMALIGN = "/bin/tmalign"


def run_tmalign(file1, file2, outputPath, tmalign):

    logger.info("Trying to execute the command: '%s %s %s'.",
                tmalign, file1, file2)

    for fname in (file1, file2):
        if not os.path.isfile(fname):
            logging.error("Missing file: %s", fname)
            raise FileNotFoundError("Missing file: %s", fname)

    try:
        if (outputPath is not None and outputPath.strip() != ""):
            if (is_directory_valid(outputPath)):
                logger.info("The superposition files will be saved "
                            "at the directory '%s'" % outputPath)

                filename = os.path.split(os.path.basename(file1))[1]
                outputFile = "%s/%s.sup" % (outputPath, filename)
                args = [tmalign, file1, file2, "-o", outputFile]
        else:
            args = [tmalign, file1, file2]

        output = subprocess.check_output(args)
    except subprocess.CalledProcessError as e:
        logger.exception("%s TMalign failed (returned %s):\n%s"
                         % (e.returncode, e.output))

        raise RuntimeError("TMalign failed for PDB files: %s %s"
                           % (file1, file2))

    return output.decode()


def get_seq_records(tmOutput, refId, eqvId):
    """Create a pair of SeqRecords from TMalign output."""

    logger.info("Parsing the TMalign output.")

    lines = tmOutput.splitlines()

    # Extract the TM-score (measure of structure similarity)
    # Take the mean of the (two) given TM-scores -- not sure which is reference
    tmScores = []
    for line in lines:
        if line.startswith('TM-score'):
            # TMalign v. 2012/05/07 or earlier
            tmScores.append(float(line.split(None, 2)[1]))
        elif 'TM-score=' in line:
            # TMalign v. 2013/05/11 or so
            tokens = line.split()
            for token in tokens:
                if token.startswith('TM-score='):
                    _key, _val = token.split('=')
                    tmScores.append(float(_val.rstrip(',')))
                    break

    tmScore = math.fsum(tmScores) / len(tmScores)
    # Extract the sequence alignment
    lastLines = lines[-7:]

    assert lastLines[0].startswith('(":"')  # (":" denotes the residues pairs
    assert lastLines[-1].startswith('Total running time is')

    refSeq, eqvSeq = lastLines[1].strip(), lastLines[3].strip()

    return (SeqRecord(Seq(refSeq), id=refId,
                      description="TMalign TM-score=%f" % tmScore),
            SeqRecord(Seq(eqvSeq), id=eqvId,
                      description="TMalign TM-score=%f" % tmScore),
            )


def align_2struct(file1, file2, outputPath=None, tmalign=None):

    if (tmalign is None):
        tmalign = TMALIGN

    tmOutput = run_tmalign(file1, file2, outputPath, tmalign)

    tmSeqPair = get_seq_records(tmOutput, file1, file2)

    alignment = MultipleSeqAlignment(tmSeqPair, generic_protein)

    return alignment


def extract_chain_from_sup(supFile, extractChain, newChainName,
                           outputFile, QUIET=False):
    """ Extract a chain from the superposition file generated with TMAlign.
        TMAlign modifies the name of the chains, so it is highly
        recommended to rename a chain's name before to create the output file.

        @param supFile: a superposition file generated with TMAlign.
        @type supFile: string

        @param extractChain: target chain to be extracted.
        @type extractChain: string

        @param newChainName: the new name to the extracted chain.
        @type newChainName: string

        @param outputFile: the name to the extracted PDB file.
        @type  outputFile: string

        @param QUIET: mutes warning messages generated by Biopython.
        @type QUIET: boolean
    """
    from bio.pdb import (try_parse_from_pdb, try_save_2pdb)

    if (QUIET):
        try:
            import warnings
            warnings.filterwarnings("ignore")
            logger.info("Quiet mode activated. From now on, "
                        "no warning will be printed.")
        except Exception:
            logger.warning("Quiet mode could not be activated.")

    try:
        if (is_file_valid(supFile)):
            logger.info("Trying to parse the file '%s'." % supFile)

            structure = try_parse_from_pdb("SUP", supFile)

            model = structure[0]
            if (len(model.child_list) != 2):
                raise InvalidSuperpositionFileError("This structure has %d"
                                                    " chains."
                                                    " The file generated"
                                                    " by TMAlign must have"
                                                    " 2 chains."
                                                    % len(model.child_list))

            chainToRemove = 'B' if (extractChain == "A") else "A"
            model.detach_child(chainToRemove)
            if (extractChain != newChainName):
                model[extractChain].id = newChainName
            logger.info("Modifications completed.")

            logger.info("Now, it will try to parse the file '%s'." % supFile)
            try_save_2pdb(structure, outputFile)
            logger.info("File '%s' created successfully." % outputFile)
    except Exception as e:
        logger.exception(e)
        raise


def remove_sup_files(path):
    """ Remove all superposition files created by TMAlign at a defined directory.

        @param path: a path to remove superposition files.
        @type path: string
    """
    import glob
    from os import remove

    try:
        if (is_directory_valid(path)):
            targets = ['*.sup', '*.sup_atm', '*.sup_all',
                       '*.sup_all_atm', '*.sup_all_atm_lig']

            for target in targets:
                files = glob.glob('%s/%s' % (path, target))
                for file in files:
                    try:
                        remove(file)
                    except Exception as e:
                        logger.exception(e)
                        logger.warning("File %s not removed." % file)
    except Exception as e:
        logger.exception(e)
        raise
