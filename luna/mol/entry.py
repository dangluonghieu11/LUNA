import re
import logging
from operator import xor
from os.path import exists

from rdkit.Chem import Mol as RDMol
from openbabel import OBMol
from openbabel.pybel import readfile
from openbabel.pybel import Molecule as PybelMol
from openbabel.pybel import informats as OB_FORMATS

from luna.mol.wrappers.rdkit import RDKIT_FORMATS, read_multimol_file, read_mol_from_file
from luna.mol.wrappers.base import MolWrapper
from luna.util.default_values import ACCEPTED_MOL_OBJ_TYPES, ENTRY_SEPARATOR
from luna.util.file import get_file_format, get_filename
from luna.util.exceptions import InvalidEntry, IllegalArgumentError, MoleculeObjectError, MoleculeObjectTypeError, MoleculeNotFoundError
from luna.MyBio.PDB.PDBParser import PDBParser, WATER_NAMES, DEFAULT_CHAIN_ID
from luna.MyBio.PDB.Entity import Entity


logger = logging.getLogger()

# Source: https://richjenks.com/filename-regex/
FILENAME_REGEX = re.compile(r"^(?!.{256,})(?!(aux|clock\$|con|nul|prn|com[1-9]|lpt[1-9])(?:$|\.))[^ ][ \.\w\-$()+=[\];#@~,&']+[^\. ]$",
                            flags=re.IGNORECASE)  # Case insentive pattern

PCI_ENTRY_REGEX = re.compile(r'^.{1,255}:\w:\w[\w+\-]{0,2}:\-?\d{1,4}[a-zA-z]?$')
PPI_ENTRY_REGEX = re.compile(r'^.{1,255}:\w$')

REGEX_RESNUM_ICODE = re.compile(r'^([\-\+]?\d+)([a-zA-z]?)$')


class Entry:

    def __init__(self, pdb_id, chain_id, comp_name=None, comp_num=None, comp_icode=None, is_hetatm=True, sep=ENTRY_SEPARATOR):

        if xor(comp_name is None, comp_num is None):
            raise IllegalArgumentError("You tried to define a compound, so you must inform its name and number.")

        if comp_num is not None:
            try:
                assert float(comp_num).is_integer()
                comp_num = int(comp_num)
            except (ValueError, AssertionError):
                raise IllegalArgumentError("The informed compound number '%s' is invalid. It must be an integer." % str(comp_num))

        if comp_icode is not None:
            comp_icode = str(comp_icode)
            if comp_icode.isdigit() or len(comp_icode) > 1:
                raise IllegalArgumentError("The informed compound icode '%s' is invalid. It must be a character." % str(comp_icode))

        self._pdb_id = pdb_id
        self._chain_id = chain_id
        self._comp_name = comp_name
        self._comp_num = comp_num
        self._comp_icode = comp_icode
        self.is_hetatm = is_hetatm
        self.sep = sep

        if not self.is_valid():
            raise InvalidEntry("Entry '%s' does not match the PDB format." % self.to_string())

    @classmethod
    def from_string(cls, entry_str, is_hetatm=True, sep=ENTRY_SEPARATOR):
        entries = entry_str.split(sep)

        # Try to initialize a new ChainEntry.
        if len(entries) == 2:
            if any([str(i).strip() == "" for i in entries]):
                raise IllegalArgumentError("The number of fields in the informed string '%s' is incorrect. A valid ChainEntry must contain "
                                           "two obligatory fields: PDB and chain id." % entry_str)

            return cls(*entries, is_hetatm=False, sep=sep)

        # Try to initialize a new CompoundEntry.
        elif len(entries) == 4:
            if any([str(i).strip() == "" for i in entries]):
                raise IllegalArgumentError("The number of fields in the informed string '%s' is incorrect. A valid CompoundEntry "
                                           "must contain four obligatory fields: PDB, chain id, compound name, and compound "
                                           "number followed by its insertion code when applicable." % entry_str)

            # Separate ligand number from insertion code.
            matched = REGEX_RESNUM_ICODE.match(entries[3])
            if matched:
                comp_num = matched.group(1)
                try:
                    assert float(comp_num).is_integer()
                    comp_num = int(comp_num)
                except (ValueError, AssertionError):
                    raise IllegalArgumentError("The informed compound number '%s' is invalid. It must be an integer." % str(comp_num))

                icode = None if matched.group(2) == "" else matched.group(2)
                entries = entries[0:3] + [comp_num, icode]
            else:
                raise IllegalArgumentError("The compound number and its insertion code (if applicable) '%s' is invalid. "
                                           "It must be an integer followed by one insertion code character when applicable."
                                           % entries[3])
            return cls(*entries, is_hetatm=is_hetatm, sep=sep)

        else:
            raise IllegalArgumentError("The number of fields in the informed string '%s' is incorrect. A valid string must contain "
                                       "two obligatory fields (PDB and chain id) and it may contain two optional fields (compound name "
                                       "and compound number followed by its insertion code when applicable)." % entry_str)

    @property
    def pdb_id(self):
        return self._pdb_id

    @property
    def chain_id(self):
        return self._chain_id

    @property
    def comp_name(self):
        return self._comp_name

    @property
    def comp_num(self):
        return self._comp_num

    @property
    def comp_icode(self):
        if isinstance(self._comp_icode, str):
            return self._comp_icode
        else:
            return ' '

    @property
    def full_id(self):
        entry = [self.pdb_id, self.chain_id]
        if self.comp_name is not None and self.comp_num is not None:
            entry.append(self.comp_name)
            entry.append(self.comp_num)
            entry.append(self.comp_icode)
        return tuple(entry)

    def to_string(self, sep=None):
        full_id = self.full_id

        # An entry object will always have a PDB and chain id.
        entry = list(full_id[0:2])

        # If it contains additional information about the compound it will also include them.
        if len(full_id) > 2:
            if full_id[2] is not None and full_id[3] is not None:
                comp_name = str(full_id[2]).strip()
                comp_num_and_icode = str(full_id[3]).strip() + str(full_id[4]).strip()
                entry += [comp_name, comp_num_and_icode]

        sep = sep or self.sep

        return sep.join(entry)

    def is_valid(self):
        full_id = self.full_id

        if FILENAME_REGEX.match(self.pdb_id) is None:
            return False

        # Regex for ChainEntry (pdb_id, chain_id).
        if len(full_id) == 2:
            return PPI_ENTRY_REGEX.match(self.to_string(":")) is not None

        # Regex for CompoundEntry (pdb_id, chain_id, comp_name, comp_num, icode).
        elif len(full_id) == 5:
            return PCI_ENTRY_REGEX.match(self.to_string(":")) is not None

        # Return False for anything else
        return False

    def get_biopython_key(self):
        if self.comp_name is not None and self.comp_num is not None:
            if self.comp_name == 'HOH' or self.comp_name == 'WAT':
                return ('W', self.comp_num, self.comp_icode)
            elif self.is_hetatm:
                return ('H_%s' % self.comp_name, self.comp_num, self.comp_icode)
            else:
                return (' ', self.comp_num, self.comp_icode)

        return self.chain_id

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.to_string(self.sep))


class ChainEntry(Entry):

    def __init__(self, pdb_id, chain_id, sep=ENTRY_SEPARATOR):
        super().__init__(pdb_id, chain_id, is_hetatm=False, sep=sep)

    @classmethod
    def from_string(cls, entry_str, sep=ENTRY_SEPARATOR):
        entries = entry_str.split(sep)
        if len(entries) == 2:
            return cls(*entries, sep=sep)
        else:
            raise IllegalArgumentError("The number of fields in the informed string '%s' is incorrect. A valid string must contain "
                                       "two obligatory fields: PDB and chain id." % entry_str)

    @property
    def full_id(self):
        return (self.pdb_id, self.chain_id)


class CompoundEntry(Entry):

    def __init__(self, pdb_id, chain_id, comp_name, comp_num, comp_icode=None, sep=ENTRY_SEPARATOR):

        super().__init__(pdb_id, chain_id, comp_name, comp_num, comp_icode, is_hetatm=True, sep=sep)

    @classmethod
    def from_string(cls, entry_str, sep=ENTRY_SEPARATOR):
        entries = entry_str.split(sep)
        if len(entries) == 4:
            # Separate ligand number from insertion code.
            matched = REGEX_RESNUM_ICODE.match(entries[3])
            if matched:
                comp_num = matched.group(1)
                icode = None if matched.group(2) == "" else matched.group(2)
                entries = entries[0:3] + [comp_num, icode]
            else:
                raise IllegalArgumentError("The compound number and its insertion code (if applicable) '%s' is invalid. "
                                           "It must be an integer followed by one insertion code character when applicable." % entries[3])
            return cls(*entries, sep=sep)
        else:
            raise IllegalArgumentError("The number of fields in the informed string '%s' is incorrect. A valid compound entry must contain "
                                       "four obligatory fields: PDB, chain id, compound name, and compound number followed by its "
                                       "insertion code when applicable." % entry_str)

    @classmethod
    def from_file(cls, input_file, sep=":"):
        with open(input_file, "r") as IN:
            for row in IN:
                entry_str = row.strip()
                if entry_str == "":
                    continue

                yield cls.from_string(entry_str)


class MolEntry(Entry):

    def __init__(self, pdb_id, mol_id, sep=ENTRY_SEPARATOR):

        self.mol_id = mol_id

        #
        # Initialize empty properties.
        #
        self._mol_obj = None
        self.mol_file = None
        # TODO: Find a way to assume the Mol file type when not provided
        self.mol_file_ext = None
        self.mol_obj_type = None
        self.overwrite_mol_name = None
        self.is_multimol_file = None

        super().__init__(pdb_id, DEFAULT_CHAIN_ID, "LIG", 9999, is_hetatm=True, sep=sep)

    @classmethod
    def from_mol_obj(cls, pdb_id, mol_id, mol_obj, sep=ENTRY_SEPARATOR):

        if mol_obj is not None:
            if isinstance(mol_obj, MolWrapper):
                mol_obj = mol_obj.unwrap()
            elif isinstance(mol_obj, PybelMol):
                mol_obj = mol_obj.OBMol

            if isinstance(mol_obj, RDMol):
                mol_obj_type = "rdkit"
            elif isinstance(mol_obj, OBMol):
                mol_obj_type = "openbabel"
            else:
                logger.exception("Objects of type '%s' are not currently accepted." % mol_obj.__class__)
                raise MoleculeObjectTypeError("Objects of type '%s' are not currently accepted." % mol_obj.__class__)
        else:
            if mol_obj_type not in ACCEPTED_MOL_OBJ_TYPES:
                raise IllegalArgumentError("Objects of type '%s' are not currently accepted. "
                                           "The available options are: %s." % (mol_obj_type, ", ".join(ACCEPTED_MOL_OBJ_TYPES)))

        entry = cls(pdb_id, mol_id, sep)
        entry.mol_obj = mol_obj
        entry.mol_obj_type = mol_obj_type

        # TODO: Find a way to assume the Mol file type when not provided
        # mol_file_ext

        return entry

    @classmethod
    def from_mol_file(cls, pdb_id, mol_id, mol_file, is_multimol_file, mol_file_ext=None, mol_obj_type='rdkit',
                      autoload=False, overwrite_mol_name=False, sep=ENTRY_SEPARATOR):

        entry = cls(pdb_id, mol_id, sep)

        entry.mol_file = mol_file
        entry.is_multimol_file = is_multimol_file
        entry.mol_file_ext = mol_file_ext or get_file_format(mol_file)
        entry.mol_obj_type = mol_obj_type
        entry.overwrite_mol_name = overwrite_mol_name

        if autoload:
            entry._load_mol_from_file()

        return entry

    @classmethod
    def from_file(cls, input_file, pdb_id, mol_file, is_multimol_file, **kwargs):
        with open(input_file, "r") as IN:
            for row in IN:
                ligand_id = row.strip()
                if ligand_id == "":
                    continue

                yield cls.from_mol_file(pdb_id, ligand_id, mol_file, is_multimol_file, **kwargs)

    @property
    def full_id(self):
        return (self.pdb_id, self.mol_id)

    @property
    def mol_obj(self):
        if self._mol_obj is None and self.mol_file is not None:
            self._load_mol_from_file()
        return self._mol_obj

    @mol_obj.setter
    def mol_obj(self, mol_obj):
        self._mol_obj = MolWrapper(mol_obj)

    def is_valid(self):
        return True

    def is_mol_obj_loaded(self):
        return self._mol_obj is not None

    def _load_mol_from_file(self):
        logger.debug("It will try to load the molecule '%s'." % self.mol_id)

        if self.mol_file is None:
            raise IllegalArgumentError("It cannot load the molecule as no molecular file was provided.")

        available_formats = OB_FORMATS if self.mol_obj_type == "openbabel" else RDKIT_FORMATS
        tool = "Open Babel" if self.mol_obj_type == "openbabel" else "RDKit"
        if self.mol_file_ext not in available_formats:
            raise IllegalArgumentError("Extension '%s' informed or assumed from the filename is not a format "
                                       "recognized by %s." % (self.mol_file_ext, tool))

        if not exists(self.mol_file):
            raise FileNotFoundError("The file '%s' was not found." % self.mol_file)

        try:
            if self.mol_obj_type == "openbabel":
                mols = readfile(self.mol_file_ext, self.mol_file)
                # If it is a multimol file, then we need to loop over the molecules to find the target one.
                # Note that in this case, the ids must match.
                if self.is_multimol_file:
                    for ob_mol in mols:
                        if self.mol_id == get_filename(ob_mol.OBMol.GetTitle()):
                            self.mol_obj = ob_mol
                            break
                else:
                    self.mol_obj = mols.__next__()
            else:
                if self.mol_file_ext == "pdb":
                    self.mol_obj = read_mol_from_file(self.mol_file, mol_format=self.mol_file_ext, removeHs=False)
                else:
                    # If 'targets' is None, then the entire Mol file will be read.
                    targets = None
                    # If it is a multimol file than loop through it until the informed molecule (by its mol_id) is found.
                    if self.is_multimol_file:
                        targets = [self.mol_id]

                    for rdk_mol, mol_id in read_multimol_file(self.mol_file, mol_format=self.mol_file_ext, targets=targets, removeHs=False):
                        # It returns None if the molecule parsing generated errors.
                        self.mol_obj = rdk_mol
                        break
        except Exception as e:
            logger.exception(e)
            raise MoleculeObjectError("An error occurred while parsing the molecular file with %s and the molecule "
                                      "object for the entry '%s' could not be created. Check the logs for more information."
                                      % (tool, self.to_string()))

        if self._mol_obj is None:
            raise MoleculeNotFoundError("The ligand '%s' was not found in the input file or generated errors while parsing it with %s."
                                        % (self.mol_id, tool))
        else:
            if not self.mol_obj.has_name() or self.overwrite_mol_name:
                self.mol_obj.set_name(self.mol_id)

        logger.debug("Molecule '%s' was successfully loaded." % self.mol_id)

    def get_biopython_structure(self, entity=None, parser=None):

        if parser is None:
            parser = PDBParser(PERMISSIVE=True, QUIET=True, FIX_ATOM_NAME_CONFLICT=True, FIX_OBABEL_FLAGS=False)

        mol_file_ext = self.mol_file_ext
        if mol_file_ext is None and self.mol_file is not None:
            mol_file_ext = get_file_format(self.mol_file)

        if self.mol_obj_type == "openbabel":
            pdb_block = self.mol_obj.to_pdb_block()

            atm = self.mol_obj.unwrap().GetFirstAtom()
            residue_info = atm.GetResidue()

            # When the PDBParser finds an empty chain, it automatically replace it by 'z'.
            chain_id = residue_info.GetChain() if residue_info.GetChain().strip() != "" else self.chain_id
            comp_num = residue_info.GetNum()

            if residue_info.GetName() in WATER_NAMES:
                comp_name = "W"
            elif residue_info.IsHetAtom(atm):
                comp_name = "H_%s" % residue_info.GetName()
            else:
                comp_name = " "

            if mol_file_ext == "pdb":
                self.chain_id = chain_id
                self.comp_name = residue_info.GetName()
                self.comp_num = comp_num
                self.is_hetatm = residue_info.IsHetAtom(atm)
        else:
            pdb_block = self.mol_obj.to_pdb_block()

            if mol_file_ext == "pdb":
                residue_info = self.mol_obj.unwrap().GetAtoms()[0].GetPDBResidueInfo()
                # When the PDBParser finds an empty chain, it automatically replace it by 'z'.
                chain_id = residue_info.GetChainId() if residue_info.GetChainId().strip() != "" else self.chain_id
                comp_num = residue_info.GetResidueNumber()

                if residue_info.GetResidueName() in WATER_NAMES:
                    comp_name = "W"
                elif residue_info.GetIsHeteroAtom():
                    comp_name = "H_%s" % residue_info.GetResidueName()
                else:
                    comp_name = " "

                self.chain_id = chain_id
                self.comp_name = residue_info.GetResidueName()
                self.comp_num = comp_num
                self.is_hetatm = residue_info.GetIsHeteroAtom()
            else:
                # When the PDBParser finds an empty chain, it automatically replace it by 'z'.
                chain_id = self.chain_id
                comp_name = "H_UNL"
                comp_num = 1

        comp_structure = parser.get_structure_from_pdb_block(self.pdb_id, pdb_block)

        chain = comp_structure[0][chain_id]
        if self.chain_id != chain.id:
            chain.id = self.chain_id

        lig = chain[(comp_name, comp_num, " ")]

        # It only substitutes the ligand id if it is different from the id defined by the MolEntry object property.
        # This update will never happen when the ligand file is a PDB file as the ids are guaranteed to be equal.
        if lig.id != ("H_%s" % self.comp_name, self.comp_num, " "):
            lig.id = ("H_%s" % self.comp_name, self.comp_num, " ")

        lig.resname = self.comp_name

        if entity is not None:
            if isinstance(entity, Entity):
                structure = entity.get_parent_by_level('S')
                if self.chain_id not in structure[0].child_dict:
                    chain = chain.copy()
                    structure[0].add(chain)
                else:
                    lig = lig.copy()
                    # Update the ligand index according to the number of compounds already present in the chain.
                    lig.idx = len(structure[0][self.chain_id].child_list)
                    structure[0][self.chain_id].add(lig)
            else:
                raise IllegalArgumentError("The informed entity is not a valid Biopython object.")
        else:
            entity = comp_structure

        return entity

    def __repr__(self):
        return '<MolEntry: %s%s%s>' % (self.pdb_id, self.sep, self.mol_id)

    def __getstate__(self):
        if self._mol_obj is not None:
            self.mol_obj = MolWrapper(self.mol_obj)
        return self.__dict__

    def __setstate__(self, state):
        self.__dict__.update(state)


def recover_entries_from_entity(entity, get_small_molecules=True, get_chains=True, sep=ENTRY_SEPARATOR):

    if entity.level == "S":
        if get_small_molecules:
            residues = entity[0].get_residues()
        if get_chains:
            chains = entity[0].get_chains()

    elif entity.level == "M":
        if get_small_molecules:
            residues = entity.get_residues()
        if get_chains:
            chains = entity.get_chains()
    else:
        if get_small_molecules:
            # If the entity is already a Chain, get_parent_by_level() returns the same object.
            # But, if the entity is a Residue or Atom, it will return its corresponding chain parent.
            residues = entity.get_parent_by_level("C").get_residues()
        if get_chains:
            chains = entity.get_parent_by_level("M").get_chains()

    if get_small_molecules:
        pdb_id = entity.get_parent_by_level("S").id
        for res in residues:
            if res.is_hetatm():
                comp_num_and_icode = ""
                if isinstance(res.id[1], int):
                    comp_num_and_icode = str(res.id[1])
                comp_num_and_icode += str(res.id[2]) if res.id[2].strip() else ""

                yield sep.join([pdb_id, res.parent.id, res.resname, comp_num_and_icode])

    if get_chains:
        pdb_id = entity.get_parent_by_level("S").id
        for chain in chains:
            entry = sep.join([pdb_id, chain.id])
            yield entry