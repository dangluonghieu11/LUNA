from rdkit.Chem import SanitizeFlags, SanitizeMol
from openbabel.openbabel import OBSmartsPattern


from mol.wrappers.base import AtomWrapper, MolWrapper, BondType
from mol.charge_model import OpenEyeModel


import logging

logger = logging.getLogger()


class MolValidator:

    def __init__(self, charge_model=OpenEyeModel(), references=None, fix_nitro=True, fix_amidine_and_guanidine=True,
                 fix_valence=True, fix_charges=True):
        self.charge_model = charge_model
        self.references = references
        self.fix_nitro = fix_nitro
        self.fix_amidine_and_guanidine = fix_amidine_and_guanidine
        self.fix_valence = fix_valence
        self.fix_charges = fix_charges

    def validate_mol(self, mol_obj):
        if not isinstance(mol_obj, MolWrapper):
            mol_obj = MolWrapper(mol_obj)

        # Validations to be made...
        #       TODO: check aromaticity.
        #       TODO: Return list of errors

        if self.fix_nitro:
            self.fix_nitro_substructure_and_charge(mol_obj)

        if self.fix_amidine_and_guanidine:
            self.fix_amidine_and_guanidine_charges(mol_obj)

        # Check if the molecule has errors...
        is_mol_valid = True
        for atm in mol_obj.get_atoms():
            if not self.is_valence_valid(atm):
                is_mol_valid = False

            if not self.is_charge_valid(atm):
                is_mol_valid = False

        if not is_mol_valid:
            logger.warning("Invalid molecule: check the logs for more information.")

        return is_mol_valid

    def fix_nitro_substructure_and_charge(self, mol_obj):
        ob_smart = OBSmartsPattern()
        # Invalid nitro pattern.
        ob_smart.Init("[$([NX3v5]([!#8])(=O)=O)]")
        if ob_smart.Match(mol_obj.unwrap()):
            logger.warning("One or more invalid nitro substructures ('*-N(=O)=O') were found. "
                           "It will try to substitute them to '*-[N+]([O-])=O'.")

            # Iterate over each Nitro group in the molecule.
            for ids in ob_smart.GetUMapList():
                # Get the N atom.
                atm = [AtomWrapper(mol_obj.GetAtom(i)) for i in ids][0]
                for bond in atm.get_bonds():
                    partner = bond.get_partner_atom(atm)

                    if partner.get_symbol() == "O" and bond.get_bond_type() == 2:
                        # Change double bond to single bond.
                        bond.set_bond_type(BondType.SINGLE)
                        # Attributes a +1 charge to the N.
                        atm.set_charge(1)
                        # Attributes a -1 charge to the O.
                        partner.set_charge(-1)

                        # It needs to update only one of the oxygen bonds.
                        break

            ob_smart = OBSmartsPattern()
            # Valid nitro pattern.
            ob_smart.Init("[$([NX3v4+](=O)[O-])][!#8]")
            if ob_smart.Match(mol_obj.unwrap()):
                logger.warning("Invalid nitro substructures ('*-N(=O)=O') successfully substituted to '*-[N+]([O-])=O'.")

    def fix_amidine_and_guanidine_charges(self, mol_obj):
        # These errors occur with guanidine-like substructures when the molecule is ionized. It happens that the charge is
        # incorrectly assigned to the central carbon, so the guanidine-like C ends up with a +1 charge and the N with a double bond
        # to the central C ends up with a +0 charge. To fix it, we assign the correct charges to the N (+1) and C (0).

        ob_smart = OBSmartsPattern()
        # Invalid amidine and guanidine pattern.
        ob_smart.Init("[$([NH1X2v3+0](=[CH0X3+1](N)))]")
        if ob_smart.Match(mol_obj.unwrap()):
            logger.warning("One or more amidine/guanidine substructures with no charge were found. "
                           "It will try to attribute a +1 charge to the N bound to the central carbon with a double bond.")

            # Iterate over each Amidine/Guanidine group in the molecule.
            for ids in ob_smart.GetUMapList():
                # Get the N atom.
                atm = [AtomWrapper(mol_obj.GetAtom(i)) for i in ids][0]

                for bond in atm.get_bonds():
                    partner = bond.get_partner_atom(atm)

                    if partner.get_symbol() == "C":
                        # Attributes a +1 charge to the N and corrects its degree to three, i.e., the N will become '=NH2+'.
                        atm.set_charge(1)

                        # Set the number of implicit Hydrogens to 1 (=NH2+).
                        #
                        # Explanation: the above initialized Smarts pattern checks for nitrogens containing 1 explicit
                        #              hydrogen ([NH1X2v3+0]). Thus, to correctly update its valence to 4 (=NH2+),
                        #              we need to add a new implicit hydrogen.
                        atm.unwrap().SetImplicitHCount(1)

                        # Remove any charges in the C.
                        partner.set_charge(0)

            ob_smart = OBSmartsPattern()
            # Valid amidine and guanidine pattern.
            ob_smart.Init("[$([NH2X3v4+1](=[CH0X3+0](N)))]")
            if ob_smart.Match(mol_obj.unwrap()):
                logger.warning("Invalid amidine/guanidine substructures were correctly charged.")

    def is_valence_valid(self, atm):
        if not isinstance(atm, AtomWrapper):
            atm = AtomWrapper(atm)

        # Atoms other than N are not currently evaluated because we did not find any similar errors with other atoms.
        if atm.get_atomic_num() == 7:
            # It corrects quaternary ammonium Nitrogen errors.
            #
            #   While reading from PDBs containing quaternary ammonium N, it may happen to the N to be perceived as
            #       having a valence equal to 5 (v5, hypervalent). It means Open Babel has added an invalid implicit hydrogen and
            #       treated the N as a hypervalent nitrogen.
            #
            if atm.get_valence() == 5 and atm.get_charge() == 0:
                logger.warning("Atom # %d has incorrect valence and charge." % atm.get_id())

                if self.fix_valence:
                    logger.warning("'Fix valence' option is set on. It will update the valence of atom # %d "
                                   "from %d to 4 and correct its charge." % (atm.get_id(), atm.get_valence()))

                    # Set the number of implicit hydrogens to 0 and adds a +1 charge to the Nitrogen.
                    # It is necessary because Open Babel tends to add 1 implicit hydrogen to ammonium nitrogens
                    # what makes them become hypervalent (v5) and with a neutral charge.
                    atm.unwrap().SetImplicitHCount(0)
                    atm.set_charge(1)
                    return True
                return False
        return True

    def is_charge_valid(self, atm):
        if not isinstance(atm, AtomWrapper):
            atm = AtomWrapper(atm)

        expected_charge = self.get_expected_charge(atm)

        if expected_charge is not None and expected_charge != atm.get_charge():
            logger.warning("Atom # %d has incorrect charges defined." % atm.get_id())

            if self.fix_charges:
                logger.warning("'Fix charges' option is set on. It will update the charge of atom # %d from %d to %d."
                               % (atm.get_id(), atm.get_charge(), expected_charge))
                atm.set_charge(expected_charge)
                return True
            else:
                return False
        return True

    def get_expected_charge(self, atm):
        if not isinstance(atm, AtomWrapper):
            atm = AtomWrapper(atm)

        return self.charge_model.get_charge(atm)

    def compare_to_ref(self, mol_obj, ref):
        # to be implemented
        pass


class RDKitValidator:

    def __init__(self, sanitize_opts=SanitizeFlags.SANITIZE_ALL):
        self.sanitize_opts = sanitize_opts

    def is_mol_valid(self, rdk_mol):
        try:
            SanitizeMol(rdk_mol, sanitizeOps=self.sanitize_opts)
            return True
        except Exception as e:
            logger.warning(e)
            return False
