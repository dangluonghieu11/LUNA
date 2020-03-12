from math import ceil
from os.path import exists
from collections import defaultdict
from functools import wraps
import time
import multiprocessing as mp
import logging

from pybel import readfile
from pybel import informats as OB_FORMATS
from rdkit.Chem import ChemicalFeatures
from rdkit.Chem import MolFromPDBBlock, MolFromSmiles

# Local modules
from analysis.summary import *
from mol.depiction import ligand_pharm_figure
from mol.clustering import cluster_fps_butina
from mol.features import FeatureExtractor
from mol.fingerprint import generate_fp_for_mols
from mol.entry import MolEntry
from mol.groups import AtomGroupPerceiver
from mol.interaction.contact import get_contacts_for_entity
from mol.interaction.calc import InteractionCalculator
from mol.interaction.conf import InteractionConf
from mol.interaction.fp.shell import ShellGenerator
from mol.wrappers.base import MolWrapper
from mol.wrappers.rdkit import RDKIT_FORMATS, read_multimol_file
from mol.amino_features import DEFAULT_AMINO_ATM_FEATURES
from util.default_values import *
from util.exceptions import *
from util.file import pickle_data, unpickle_data, create_directory, get_file_format, get_unique_filename
from util.logging import new_logging_file, load_default_logging_conf
from util import iter_to_chunks
from util.multiprocessing_logging import start_mp_handler

from MyBio.PDB.PDBParser import PDBParser
from MyBio.selector import ResidueSelector
from MyBio.util import download_pdb, entity_to_string, get_entity_from_entry


PDB_PARSER = PDBParser(PERMISSIVE=True, QUIET=True, FIX_ATOM_NAME_CONFLICT=True, FIX_OBABEL_FLAGS=False)


class StepControl:

    def __init__(self, step_id, num_subtasks, num_executed_subtasks, is_complete=False):

        self.step_id = step_id
        self.num_subtasks = num_subtasks
        self.num_executed_subtasks = num_executed_subtasks
        self.is_complete = is_complete

    @property
    def has_subtasks(self):
        return self.num_subtasks > 1

    @property
    def progress(self):
        if self.has_subtasks:
            return (self.num_executed_subtasks / self.num_subtasks)
        else:
            return 100 if self.is_complete else 0

    def update_progress(self, step=1):
        if self.has_subtasks:
            self.num_executed_subtasks += step
            if (self.num_subtasks ==
                    self.num_executed_subtasks):
                self.is_complete = True
        else:
            self.is_complete = True

    def __repr__(self):
        return '<StepControl: [Step id=%d, Progress=%.2f%%]>' % (self.step_id, self.progress)


class ExceptionWrapper(object):

    def __init__(self, step_id, has_subtasks, is_critical, desc=None):
        self.step_id = step_id
        self.has_subtasks = has_subtasks
        self.is_critical = is_critical
        self.desc = desc

    def __call__(self, func):
        @wraps(func)
        def callable(*args, **kwargs):
            # Project class
            proj_obj = args[0]

            # TODO: update if it is critical based on the database if it is available
            task = proj_obj.get_or_create_task(self.step_id, self.has_subtasks)
            error_message = None
            try:
                # Print a description of the function to be executed
                if self.desc:
                    desc = self.desc
                else:
                    get_default_message = True

                    # If the object is an instance of a project.
                    if hasattr(proj_obj, 'job_code'):
                        if self.step_id in proj_obj.step_details:
                            desc = proj_obj.step_details[self.step_id]["description"]
                            get_default_message = False

                    if get_default_message:
                        desc = ("Running function: %s." % func_call_2str(func, *args, **kwargs))
                proj_obj.logger.info(desc)

                # If the object is an instance of a project.
                if hasattr(proj_obj, 'job_code'):
                    proj_obj.update_step_details(task)

                result = func(*args, **kwargs)

                proj_obj.logger.warning("The function '%s' finished successfully." % func.__name__)

                return
            except Exception as e:
                proj_obj.logger.warning("The function '%s' failed." % func.__name__)
                proj_obj.logger.exception(e)

                # TODO: se chegar uma mensagem de erro não user friendly,
                # coloque uma mensagem generica
                error_message = e.args[0]
                if self.is_critical:
                    proj_obj.logger.warning("As the called function was critical, the program will be aborted.")
                    raise
            finally:
                task.update_progress()

                # If the object is an instance of a project,
                # update the step details in the project related tables.
                if hasattr(proj_obj, 'job_code') is False:
                    proj_obj.update_step_details(task, error_message)

                if task.is_complete:
                    proj_obj.logger.warning("The step %d has been completed." % task.step_id)

        return callable


class Project:

    def __init__(self,
                 entries,
                 working_path,
                 pdb_path=PDB_PATH,
                 has_local_files=False,
                 overwrite_path=False,
                 db_conf_file=None,
                 pdb_template=None,
                 atom_prop_file=ATOM_PROP_FILE,
                 try_h_addition=True,
                 ph=7.4,
                 amend_mol=True,
                 mol_obj_type='rdkit',
                 inter_conf=INTERACTION_CONF,
                 inter_calc=None,
                 calc_mfp=True,
                 mfp_opts=None,
                 calc_ifp=True,
                 ifp_num_levels=7,
                 ifp_radius_step=1,
                 ifp_length=IFP_LENGTH,
                 ifp_count=False,
                 similarity_func="BulkTanimotoSimilarity",
                 preload_mol_files=False,
                 default_properties=DEFAULT_AMINO_ATM_FEATURES,
                 butina_cutoff=0.2,
                 run_from_step=None,
                 run_until_step=None,
                 nproc=None):

        if mol_obj_type not in ACCEPTED_MOL_OBJ_TYPES:
            raise IllegalArgumentError("Objects of type '%s' are not currently accepted. "
                                       "The available options are: %s." % (mol_obj_type, ", ".join(ACCEPTED_MOL_OBJ_TYPES)))

        if inter_conf is not None and isinstance(inter_conf, InteractionConf) is False:
            raise IllegalArgumentError("The informed interaction configuration must be an instance of '%s'." % InteractionConf)

        if inter_calc is not None and isinstance(inter_calc, InteractionCalculator) is False:
            raise IllegalArgumentError("The informed interaction configuration must be an instance of '%s'." % InteractionCalculator)

        self.entries = entries
        self.working_path = working_path
        self.pdb_path = pdb_path
        self.has_local_files = has_local_files
        self.overwrite_path = overwrite_path
        self.db_conf_file = db_conf_file
        self.pdb_template = pdb_template
        self.atom_prop_file = atom_prop_file
        self.ph = ph
        self.amend_mol = amend_mol
        self.mol_obj_type = mol_obj_type
        self.try_h_addition = try_h_addition

        # Interaction calculator parameters.
        self.inter_conf = inter_conf

        if inter_calc is None:
            inter_calc = InteractionCalculator(inter_conf=self.inter_conf)
        self.inter_calc = inter_calc

        # Fingerprint parameters.
        self.calc_mfp = calc_mfp
        self.mfp_opts = mfp_opts
        self.calc_ifp = calc_ifp
        self.ifp_num_levels = ifp_num_levels
        self.ifp_radius_step = ifp_radius_step
        self.ifp_length = ifp_length
        self.ifp_count = ifp_count

        self.similarity_func = similarity_func
        self.butina_cutoff = butina_cutoff
        self.default_properties = default_properties
        self.run_from_step = run_from_step
        self.run_until_step = run_until_step
        self.preload_mol_files = preload_mol_files
        self.step_controls = {}

        if nproc is None:
            nproc = mp.cpu_count() - 1
        elif nproc < 1:
            logger.warning("It was trying to create an invalid number of processes (%s). Therefore, the number of "
                           "processes 'nproc' was set to its maximum accepted capability (%d)." % (str(nproc), (mp.cpu_count() - 1)))
            nproc = mp.cpu_count() - 1
        elif nproc >= mp.cpu_count():
            logger.warning("It was trying to create %d processes, which is equal to or greater than the maximum "
                           "amount of available CPUs (%d). Therefore, the number of processes 'nproc' was set to %d "
                           "to leave at least one CPU free." % (nproc, mp.cpu_count(), (mp.cpu_count() - 1)))
            nproc = mp.cpu_count() - 1
        self.nproc = nproc

        load_default_logging_conf()

        self.log_preferences()

    def __call__(self):
        raise NotImplementedError("This class is not callable. Use a class that implements this method.")

    def run(self):
        self()

    def log_preferences(self):
        logger.info("New project initialized...")
        params = ["\t\t-- %s = %s" % (key, str(self.__dict__[key])) for key in sorted(self.__dict__)]
        logger.info("Preferences:\n%s" % "\n".join(params))

    def prepare_project_path(self):
        logger.warning("Initializing path '%s'..." % self.working_path)

        create_directory(self.working_path, self.overwrite_path)
        create_directory("%s/pdbs" % self.working_path)
        create_directory("%s/figures" % self.working_path)
        create_directory("%s/results" % self.working_path)
        create_directory("%s/logs" % self.working_path)
        create_directory("%s/tmp" % self.working_path)

        logger.warning("Path '%s' created successfully!!!" % self.working_path)

    def init_logging_file(self, logging_filename=None, use_mp_handler=True):
        if not logging_filename:
            logging_filename = get_unique_filename(TMP_FILES)

        try:
            new_logging_file(logging_filename)

            if use_mp_handler:
                start_mp_handler()

            logger.warning("Logging file initialized successfully.")

            # Print preferences at the new logging file.
            self.log_preferences()
        except Exception as e:
            logger.exception(e)
            raise FileNotCreated("Logging file could not be created.")

    def validate_entry_format(self, target_entry):
        if not target_entry.is_valid():
            raise InvalidEntry("Entry '%s' does not match a LUNA's entry format." % target_entry.to_string())

    def get_pdb_file(self, pdb_id):
        pdb_file = "%s/%s.pdb" % (self.pdb_path, pdb_id)

        if self.has_local_files:
            if not exists(pdb_file):
                raise FileNotFoundError("The PDB file '%s' was not found." % pdb_file)
        elif not exists(pdb_file):
            working_pdb_path = "%s/pdbs" % self.working_path
            pdb_file = "%s/%s.pdb" % (working_pdb_path, pdb_id)

            try:
                download_pdb(pdb_id=pdb_id, output_path=working_pdb_path)
            except Exception as e:
                logger.exception(e)
                raise FileNotCreated("PDB file '%s' was not created." % pdb_file) from e

        return pdb_file

    def decide_hydrogen_addition(self, pdb_header):
        if self.try_h_addition:
            if "structure_method" in pdb_header:
                method = pdb_header["structure_method"]
                # If the method is not a NMR type does not add hydrogen as it usually already has hydrogens.
                if method.upper() in NMR_METHODS:
                    logger.exception("The structure related to the entry '%s' was obtained by NMR, so it will "
                                     "not add hydrogens to it." % self.current_entry)
                    return False
            return True
        return False

    def perceive_chemical_groups(self, entity, ligand, add_h=False):
        feature_factory = ChemicalFeatures.BuildFeatureFactory(self.atom_prop_file)
        feature_extractor = FeatureExtractor(feature_factory)

        perceiver = AtomGroupPerceiver(feature_extractor, add_h=add_h, ph=self.ph, amend_mol=self.amend_mol,
                                       mol_obj_type=self.mol_obj_type, default_properties=self.default_properties,
                                       tmp_path="%s/tmp" % self.working_path)

        radius = self.inter_conf.boundary_cutoff or BOUNDARY_CONF.boundary_cutoff
        nb_compounds = get_contacts_for_entity(entity, ligand, level='R', radius=radius)

        mol_objs_dict = {}
        if isinstance(self.current_entry, MolEntry):
            mol_objs_dict[self.current_entry.get_biopython_key()] = self.current_entry.mol_obj

        atm_grps_mngr = perceiver.perceive_atom_groups(set([x[1] for x in nb_compounds]), mol_objs_dict=mol_objs_dict)

        logger.info("Chemical group perception finished!!!")

        return atm_grps_mngr

    def get_rdkit_mol(self, entity, target, mol_name="Mol0"):
        target_sel = ResidueSelector({target})
        pdb_block = entity_to_string(entity, target_sel, write_conects=False)
        rdmol = MolFromPDBBlock(pdb_block)
        rdmol.SetProp("_Name", mol_name)

        return rdmol

    def add_mol_obj_to_entries(self):
        mol_files = defaultdict(dict)
        for entry in self.entries:
            if not entry.is_mol_obj_loaded():
                entry.mol_obj_type = self.mol_obj_type
                mol_files[(entry.mol_file, entry.mol_file_ext)][entry.mol_id] = entry
            else:
                logger.info("Molecular object in entry %s was manually informed and will not be reloaded." % entry)

        tool = "Open Babel" if self.mol_obj_type == "openbabel" else "RDKit"
        logger.info("It will try to preload the molecular objects using %s. Total of files to be read: %d."
                    % (tool, len(mol_files)))

        try:
            for mol_file, mol_file_ext in mol_files:
                key = (mol_file, mol_file_ext)
                ext = mol_file_ext or get_file_format(mol_file)

                available_formats = OB_FORMATS if self.mol_obj_type == "openbabel" else RDKIT_FORMATS
                if ext not in available_formats:
                    raise IllegalArgumentError("Extension '%s' informed or assumed from the filename is not a format "
                                               "recognized by %s." % (ext, tool))

                if not exists(mol_file):
                    raise FileNotFoundError("The file '%s' was not found." % mol_file)

                logger.info("Reading the file '%s'. The number of target entries located in this file is %d."
                            % (mol_file, len(mol_files[key])))

                try:
                    if self.mol_obj_type == "openbabel":
                        for ob_mol in readfile(ext, mol_file):
                            mol_id = ob_mol.OBMol.GetTitle().strip()
                            if mol_id in mol_files[key]:
                                entry = mol_files[key][mol_id]
                                entry.mol_obj = ob_mol
                                del(mol_files[key][mol_id])
                                logger.info("A structure to the entry '%s' was found in the file '%s' and loaded "
                                            "into the entry." % (entry, mol_file))

                                # If there is no other molecules to search, just stop the loop.
                                if len(mol_files[key]) == 0:
                                    break
                        else:
                            logger.info("All target ligands located in the file '%s' were successfully loaded." % mol_file)
                    else:
                        targets = list(mol_files[key].keys())
                        for (i, rdk_mol) in enumerate(read_multimol_file(mol_file, ext, targets=targets, removeHs=False)):
                            mol_id = targets[i]
                            entry = mol_files[key][mol_id]
                            # It returns None if the molecule parsing generated errors.
                            if rdk_mol:
                                entry.mol_obj = rdk_mol
                                del(mol_files[key][mol_id])
                                logger.info("A structure to the entry '%s' was found in the file '%s' and loaded "
                                            "into the entry." % (entry, mol_file))
                            else:
                                logger.warning("The structure related to the entry '%s' was found in the file '%s', but it could "
                                               "not be loaded as errors were found while parsing it." % (entry, mol_file))
                except Exception as e:
                    logger.exception(e)
                    raise MoleculeObjectError("An error occurred while parsing the molecular file '%s' with %s and the molecule "
                                              "objects could not be created. Check the logs for more information." %
                                              (mol_file, tool))
        except Exception as e:
            logger.exception(e)
            raise

        invalid_entries = [e for m in mol_files for e in mol_files[m].values()]
        if invalid_entries:
            entries = set(self.entries)
            for entry in invalid_entries:
                entries.remove(entry)
                logger.warning("Entry '%s' was not found or generated errors, so it will be removed from the entries list."
                               % entry)
            logger.warning("%d invalid entries removed." % len(invalid_entries))
            self.entries = entries

    def generate_ligand_figure(self, rdmol, group_types):
        atm_types = defaultdict(set)
        atm_map = {}
        for atm in rdmol.GetAtoms():
            atm_map[atm.GetPDBResidueInfo().GetSerialNumber()] = atm.GetIdx()

        for grp in group_types.atm_grps:
            for atm in grp.atoms:
                atm_id = atm_map[atm.serial_number]
                atm_types[atm_id].update(set(grp.chemicalFeatures))

        output = "%s/figures/%s.svg" % (self.working_path,
                                        rdmol.GetProp("_Name"))
        ligand_pharm_figure(rdmol, atm_types, output, ATOM_TYPES_COLOR)

    def clusterize_ligands(self, fingerprints):
        fps_only = [x["fp"] for x in fingerprints]

        try:
            clusters = cluster_fps_butina(fps_only, cutoff=self.butina_cutoff, similarity_func=self.similarity_func)
        except Exception:
            raise ProcessingFailed("Clustering step failed.")

        lig_clusters = {}
        for i, cluster in enumerate(clusters):
            for mol_id in cluster:
                lig_clusters[fingerprints[mol_id]["mol"]] = i

        return lig_clusters

    def save(self, output_file, compressed=True):
        pickle_data(self, output_file, compressed)

    @staticmethod
    def load(input_file):
        return unpickle_data(input_file)


class FingerprintProject(Project):

    def __init__(self, entries, working_path, mfp_output=None, ifp_output=None, **kwargs):
        self.mfp_output = mfp_output
        self.ifp_output = ifp_output

        super().__init__(entries=entries, working_path=working_path, **kwargs)

    def _process_entries(self, entries):

        for target_entry in entries:
            try:
                logger.info("Starting processing entry %s." % target_entry)
                self.current_entry = target_entry

                # # Check if the entry is in the correct format.
                # # It also accepts entries whose pdb_id is defined by the filename.
                if isinstance(target_entry, MolEntry) is False:
                    self.validate_entry_format(target_entry)

                if self.calc_ifp:
                    # # TODO: allow the person to pass a pdb_file into entries.
                    pdb_file = self.get_pdb_file(target_entry.pdb_id)

                    structure = PDB_PARSER.get_structure(target_entry.pdb_id, pdb_file)
                    add_hydrogen = self.decide_hydrogen_addition(PDB_PARSER.get_header())

                    if isinstance(target_entry, MolEntry):
                        structure = target_entry.get_biopython_structure(structure, PDB_PARSER)

                    ligand = get_entity_from_entry(structure, target_entry)
                    ligand.set_as_target(is_target=True)

                    atm_grps_mngr = self.perceive_chemical_groups(structure[0], ligand, add_hydrogen)

                    #
                    # Calculate interactions
                    #
                    interactions_mngr = self.inter_calc.calc_interactions(atm_grps_mngr.atm_grps)

                    # Create hydrophobic islands.
                    atm_grps_mngr.merge_hydrophobic_atoms(interactions_mngr)

                result = {"id": (target_entry.to_string())}

                # TODO: It is not accepting Fingerprint types other than Morgan fp.
                if self.calc_mfp:
                    if isinstance(target_entry, MolEntry):
                        rdmol_lig = MolFromSmiles(MolWrapper(target_entry.mol_obj).to_smiles())
                        rdmol_lig.SetProp("_Name", target_entry.mol_id)
                        result["mfp"] = generate_fp_for_mols([rdmol_lig], "morgan_fp")[0]["fp"]
                    else:
                        # Read from PDB.
                        pass

                if self.calc_ifp:
                    shells = ShellGenerator(self.ifp_num_levels, self.ifp_radius_step)
                    sm = shells.create_shells(atm_grps_mngr)

                    unique_shells = not self.ifp_count
                    result["ifp"] = sm.to_fingerprint(fold_to_size=self.ifp_length, unique_shells=unique_shells, count_fp=self.ifp_count)

                if isinstance(target_entry, MolEntry):
                    result["smiles"] = MolWrapper(target_entry.mol_obj).to_smiles()
                else:
                    # TODO: Get Smiles from PDB
                    result["smiles"] = ""
                    pass
                self.result.append(result)
                logger.warning("Processing of entry %s finished successfully." % target_entry)
            except Exception as e:
                logger.exception(e)
                logger.warning("Processing of entry %s failed. Check the logs for more information." % target_entry)

    def __call__(self):
        start = time.time()

        if not self.calc_mfp and not self.calc_ifp:
            logger.critical("Both molecular and interaction fingerprints were set off. So, there is nothing to be done...")
            return

        self.prepare_project_path()
        self.init_logging_file("%s/logs/project.log" % self.working_path)

        if self.preload_mol_files:
            self.add_mol_obj_to_entries()

        manager = mp.Manager()
        self.mfps = []
        self.ifps = []
        self.interactions = manager.list()
        self.neighborhoods = []

        start = time.time()
        chunk_size = ceil(len(self.entries) / (mp.cpu_count() - 1))
        chunks = iter_to_chunks(self.entries, chunk_size)

        processes = []
        for (i, l) in enumerate(chunks):
            p = mp.Process(name="Chunk %d" % i, target=self._process_entries, args=(l,))
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        if self.calc_mfp:
            self.mfp_output = self.mfp_output or "%s/results/mfp.csv" % self.working_path
            with open(self.mfp_output, "w") as OUT:
                OUT.write("ligand_id,smiles,on_bits\n")
                for r in self.result:
                    if "mfp" in r:
                        fp_str = "\t".join([str(x) for x in r["mfp"].GetOnBits()])
                        OUT.write("%s,%s,%s\n" % (r["id"], r["smiles"], fp_str))

        if self.calc_ifp:
            self.ifp_output = self.ifp_output or "%s/results/ifp.csv" % self.working_path

            with open(self.ifp_output, "w") as OUT:
                if self.ifp_count:
                    OUT.write("ligand_id,smiles,on_bits,count\n")
                else:
                    OUT.write("ligand_id,smiles,on_bits\n")

                for r in self.result:
                    if "ifp" in r:

                        if self.ifp_count:
                            fp_bits_str = "\t".join([str(idx) for idx in r["ifp"].counts.keys()])
                            fp_count_str = "\t".join([str(count) for count in r["ifp"].counts.values()])
                            OUT.write("%s,%s,%s,%s\n" % (r["id"], r["smiles"], fp_bits_str, fp_count_str))
                        else:
                            fp_bits_str = "\t".join([str(x) for x in r["ifp"].get_on_bits()])
                            OUT.write("%s,%s,%s\n" % (r["id"], r["smiles"], fp_bits_str))

        end = time.time()
        logger.info("Processing finished!!!")
        logger.info("Check the results at '%s'." % self.working_path)
        logger.info("Processing time: %.2fs." % (end - start))


class LocalProject(Project):

    def __init__(self, entries, working_path, has_local_files=False, **kwargs):
        super().__init__(entries=entries, working_path=working_path, has_local_files=has_local_files, **kwargs)

    def __call__(self):

        self.prepare_project_path()
        self.init_logging_file("%s/logs/project.log" % self.working_path)

        if self.preload_mol_files:
            self.add_mol_obj_to_entries()

        manager = mp.Manager()
        self.interactions = manager.list()
        self.neighborhoods = manager.list()

        self.mfps = []
        self.ifps = []

        start = time.time()

        chunk_size = ceil(len(self.entries) / self.nproc)
        chunks = iter_to_chunks(self.entries, chunk_size)

        processes = []
        for (i, l) in enumerate(chunks):
            p = mp.Process(name="Chunk %d" % i, target=self._process_entries, args=(l,))
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        self.interactions = list(self.interactions)
        self.neighborhoods = list(self.neighborhoods)

        end = time.time()
        logger.info("Processing finished!!!")
        logger.info("Check the results at '%s'." % self.working_path)
        logger.info("Processing time: %.2fs." % (end - start))

    def _process_entries(self, entries):

        # Loop over each entry.
        for target_entry in entries:
            try:
                logger.info("Processing entry: %s." % target_entry)

                self.current_entry = target_entry

                # Check if the entry is in the correct format.
                # It also accepts entries whose pdb_id is defined by the filename.
                if isinstance(target_entry, MolEntry) is False:
                    self.validate_entry_format(target_entry)

                # TODO: allow the user to pass a pdb_file into entries.
                pdb_file = self.get_pdb_file(target_entry.pdb_id)
                target_entry.pdb_file = pdb_file

                structure = PDB_PARSER.get_structure(target_entry.pdb_id, pdb_file)
                add_hydrogen = self.decide_hydrogen_addition(PDB_PARSER.get_header())

                if isinstance(target_entry, MolEntry):
                    structure = target_entry.get_biopython_structure(structure, PDB_PARSER)

                ligand = get_entity_from_entry(structure, target_entry)
                ligand.set_as_target(is_target=True)

                atm_grps_mngr = self.perceive_chemical_groups(structure[0], ligand, add_hydrogen)

                self.neighborhoods.append((target_entry, atm_grps_mngr))

                #
                # Calculate interactions
                #
                interactions_mngr = self.inter_calc.calc_interactions(atm_grps_mngr.atm_grps)

                # Create hydrophobic islands.
                atm_grps_mngr.merge_hydrophobic_atoms(interactions_mngr)

                self.interactions.append((target_entry, interactions_mngr))

                logger.warning("Processing of entry %s finished successfully." % target_entry)

            except Exception as e:
                logger.exception(e)
                logger.warning("Processing of entry %s failed. Check the logs for more information." % target_entry)
