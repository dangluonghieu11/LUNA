import logging
from openbabel import etab
from io import StringIO
from shutil import move as rename_pdb_file
from itertools import product

from util.exceptions import IllegalArgumentError, MoleculeNotFoundError, ChainNotFoundError
from util.file import is_directory_valid
from util.default_values import ENTRY_SEPARATOR
from MyBio.PDB.PDBList import PDBList
from MyBio.PDB.PDBIO import Select
from MyBio.PDB.PDBIO import PDBIO
from MyBio.PDB.PDBParser import PDBParser
from util.exceptions import FileNotCreated, PDBNotReadError


logger = logging.getLogger()


def download_pdb(pdb_id, output_path=".", output_file=None, overwrite=False):
    """Download a PDB file from RCSB.org.

        @param pdb_id: 4-symbols structure Id from PDB (e.g. 3J92).
        @type pdb_code: string

        @param output_path: put the PDB file in this directory.
        @type  output_path: string
    """
    logger.info("Trying to download the PDB '%s' and store it at the directory '%s'."
                % (pdb_id, output_path))

    try:
        pdb_id = pdb_id.lower()
        if (pdb_id is not None and pdb_id.strip() != ""):
            if (is_directory_valid(output_path)):
                pdbl = PDBList()
                pdbl.retrieve_pdb_file(pdb_id, pdir=output_path, file_format="pdb", overwrite=overwrite)

                if output_file:
                    pdb_file = '%s/pdb%s.ent' % (output_path, pdb_id)
                    rename_pdb_file(pdb_file, output_file)
        else:
            raise IllegalArgumentError("Inform a non empty PDB id")
    except Exception as e:
        logger.exception(e)
        raise

    logger.info("Download complete!!")


def parse_from_file(id, file):
    """Read a PDB file and return a Structure object.

        @param id: the id that will be used for the structure
        @type id: string

        @param file: name of the PDB file
        @type file: string
    """
    try:
        parser = PDBParser(PERMISSIVE=1, QUIET=True)
        structure = parser.get_structure(id, file)
        return structure
    except Exception as e:
        logger.exception(e)
        raise PDBNotReadError("File '%s' not parsed as a PDB file.")


def save_to_file(entity, output_file, select=Select(), write_conects=True, write_end=True,
                 preserve_atom_numbering=True):
    """ Write a Structure object (or a subset of a Structure object) into a file.

        @param entity: the PDB object to be saved
        @type entity: object

        @param output_file: the name of the new PDB file
        @type output_file: string

        @param select: a filtering function. Default: it extracts everything
        @type select: Select

        @param write_conects: decide if it is necessary to write CONECT fields.
        @type write_conects: boolean

        @param write_end: decide if it is necessary to write END fields.
        @type write_end: boolean

        @param preserve_atom_numbering: decide if it is necessary to re-enumerate the atom serial numbers.
        @type preserve_atom_numbering: boolean
    """
    try:
        io = PDBIO()
        io.set_structure(entity)
        io.save(output_file, select=select, write_conects=write_conects, write_end=write_end,
                preserve_atom_numbering=preserve_atom_numbering)
    except Exception as e:
        logger.exception(e)
        raise FileNotCreated("PDB file '%s' could not be created." % output_file)


def entity_to_string(entity, select=Select(), write_conects=True, write_end=True, preserve_atom_numbering=True):
    fh = StringIO()
    io = PDBIO()
    io.set_structure(entity)
    io.save(fh, select=select, write_conects=write_conects, write_end=write_end,
            preserve_atom_numbering=preserve_atom_numbering)
    fh.seek(0)
    return ''.join(fh.readlines())


def get_entity_level_name():
    return {
        "A": "atom",
        "R": "residue",
        "C": "chain",
        "M": "model",
        "S": "structure"
    }


def get_entity_from_entry(entity, entry, model=0):
        structure = entity.get_parent_by_level("S")
        model = structure[model]

        if entry.chain_id in model.child_dict:
            chain = model[entry.chain_id]

            if entry.comp_name and entry.comp_num:
                ligand_key = entry.get_biopython_key()
                if ligand_key in chain.child_dict:
                    target_entity = chain[ligand_key]
                else:
                    raise MoleculeNotFoundError("Ligand '%s' does not exist in the PDB '%s'."
                                                % (entry.to_string(ENTRY_SEPARATOR), structure.get_id()))
            else:
                target_entity = chain
        else:
            raise ChainNotFoundError("The informed chain id '%s' for the ligand entry '%s' does not exist in the PDB '%s'." %
                                     (entry.chain_id, entry.to_string(ENTRY_SEPARATOR), structure.get_id()))

        return target_entity


def get_residue_neighbors(residue, select=Select()):

    # TODO: add index as a property of the residue.
    for idx, res in enumerate(residue.parent.child_list):
        if residue == res:
            break

    # Get valid atoms according to the provided selection function.
    trgt_res_atms = {atm.name: atm for atm in residue.get_atoms() if select.accept_atom(atm)}

    print(trgt_res_atms.keys())
    print()

    if residue.is_aminoacid():
        if "N" not in trgt_res_atms:
            logger.warning("There is a missing N in the residue %s. It may have been filtered out by the provided "
                           "selection function. So, the predecessor residue cannot be identified." % residue)
        if "C" not in trgt_res_atms:
            logger.warning("There is a missing C in the residue %s. It may have been filtered out by the provided "
                           "selection function. So, the successor residue cannot be identified." % residue)

        # If neither N and C are available in the valid atom list.
        if "N" not in trgt_res_atms and "C" not in trgt_res_atms:
            return {}

        neighbors = {}
        # If the chain has a residue coming before the target residue.
        if idx - 1 >= 0:
            # First residue before the target in the chain list.
            prev_res = residue.parent.child_list[idx - 1]
            # Get valid atoms according to the provided selection function.
            prev_res_atms = {atm.name: atm for atm in prev_res.get_atoms() if select.accept_atom(atm)}

            # A peptide bond exists between the C of one amino acid and the N of another.
            if "C" in prev_res_atms and "N" in trgt_res_atms:
                # Distance atom-atom
                dist = trgt_res_atms["N"] - prev_res_atms["C"]
                # Covalent radius
                cov1 = etab.GetCovalentRad(etab.GetAtomicNum(trgt_res_atms["N"].element))
                cov2 = etab.GetCovalentRad(etab.GetAtomicNum(prev_res_atms["C"].element))
                # OpenBabel thresholds.
                if 0.4 <= dist <= cov1 + cov2 + 0.45:
                    neighbors["previous"] = prev_res
                else:
                    logger.warning("The first residue before %s is too distant to fulfill the covalent thresholds. "
                                   "It may be an indication of bad atom positioning or that there are missing residues." % residue)
            elif "C" not in prev_res_atms:
                logger.warning("There is a missing C in %s, the first residue before %s in the chain list. "
                               "It may have been filtered out by the provided selection function. "
                               "So, the predecessor residue cannot be identified." % (prev_res, residue))
        # Otherwise, it could mean that the residue is the first one in the sequence or there are missing residues.
        else:
            logger.warning("The residue %s seems not to have any predecessor residue. "
                           "It may be the first in the chain sequence or there are missing residues." % residue)

        # If the chain has a residue coming after the target residue.
        if idx + 1 < len(residue.parent.child_list):
            # First residue after the target in the chain list.
            next_res = residue.parent.child_list[idx + 1]
            # Get valid atoms according to the provided selection function.
            next_res_atms = {atm.name: atm for atm in next_res.get_atoms() if select.accept_atom(atm)}

            # A peptide bond exists between the C of one amino acid and the N of another.
            if "C" in trgt_res_atms and "N" in next_res_atms:
                # Distance atom-atom
                dist = next_res_atms["N"] - trgt_res_atms["C"]
                # Covalent radius
                cov1 = etab.GetCovalentRad(etab.GetAtomicNum(next_res_atms["N"].element))
                cov2 = etab.GetCovalentRad(etab.GetAtomicNum(trgt_res_atms["C"].element))

                # OpenBabel thresholds.
                if 0.4 <= dist <= cov1 + cov2 + 0.45:
                    neighbors["next"] = next_res
                else:
                    logger.warning("The first residue after %s is too distant to fulfill the covalent thresholds. "
                                   "It may be an indication of bad atom positioning or that there are missing residues." % residue)
            elif "N" in next_res_atms:
                logger.warning("There is a missing N in %s, the first residue after %s in the chain list. "
                               "It may have been filtered out by the provided selection function. "
                               "So, the predecessor residue cannot be identified." % (prev_res, residue))
        # Otherwise, it could mean that the residue is the last one in the sequence or there are missing residues.
        else:

            logger.warning("The residue %s seems not to have any successor residue. "
                           "It may be the last in the chain sequence or there are missing residues." % residue)

        return neighbors
    else:
        # First residue before the target in the chain list.
        prev_res = residue.parent.child_list[idx - 1]
        # Get valid atoms according to the provided selection function.
        prev_res_atms = {atm.name: atm for atm in prev_res.get_atoms() if select.accept_atom(atm)}

        neighbors = {}
        # If the chain has a residue coming before the target residue.
        if idx - 1 >= 0:
            # First residue before the target in the chain list.
            prev_res = residue.parent.child_list[idx - 1]
            # Get valid atoms according to the provided selection function.
            prev_res_atms = {atm.name: atm for atm in prev_res.get_atoms() if select.accept_atom(atm)}

            for prev_atm, trgt_atm in product(prev_res_atms.values(), trgt_res_atms.values()):
                # Distance atom-atom
                dist = trgt_atm - prev_atm
                # Covalent radius
                cov1 = etab.GetCovalentRad(etab.GetAtomicNum(trgt_atm.element))
                cov2 = etab.GetCovalentRad(etab.GetAtomicNum(prev_atm.element))

                # OpenBabel thresholds.
                if 0.4 <= dist <= cov1 + cov2 + 0.45:
                    neighbors["previous"] = prev_res
                    break

            if "previous" not in neighbors:
                logger.warning("The first residue after %s is too distant to fulfill the covalent thresholds. "
                               "It may be an indication of bad atom positioning or that there are missing residues." % residue)
        # Otherwise, it could mean that the residue is the first one in the sequence or there are missing residues.
        else:
            logger.warning("The residue %s seems not to have any predecessor residue. "
                           "It may be the first in the chain sequence or there are missing residues." % residue)

        # If the chain has a residue coming after the target residue.
        if idx + 1 < len(residue.parent.child_list):
            # First residue after the target in the chain list.
            next_res = residue.parent.child_list[idx + 1]
            # Get valid atoms according to the provided selection function.
            next_res_atms = {atm.name: atm for atm in next_res.get_atoms() if select.accept_atom(atm)}

            # Check each pair of atoms for covalently bonded atoms.
            for next_atm, trgt_atm in product(next_res_atms.values(), trgt_res_atms.values()):
                # Distance atom-atom
                dist = trgt_atm - next_atm
                # Covalent radius
                cov1 = etab.GetCovalentRad(etab.GetAtomicNum(trgt_atm.element))
                cov2 = etab.GetCovalentRad(etab.GetAtomicNum(next_atm.element))

                # OpenBabel thresholds.
                if 0.4 <= dist <= cov1 + cov2 + 0.45:
                    neighbors["next"] = next_res
                    break

            if "next" not in neighbors:
                logger.warning("The first residue after %s is too distant to fulfill the covalent thresholds. "
                               "It may be an indication of bad atom positioning or that there are missing residues." % residue)
        # Otherwise, it could mean that the residue is the last one in the sequence or there are missing residues.
        else:
            logger.warning("The residue %s seems not to have any successor residue. "
                           "It may be the last in the chain sequence or there are missing residues." % residue)

        return neighbors
