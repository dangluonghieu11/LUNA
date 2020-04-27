import sys
from os.path import exists
from collections import defaultdict
import time
import multiprocessing as mp
import logging
from threading import Thread
from queue import Queue

from openbabel.pybel import readfile
from openbabel.pybel import informats as OB_FORMATS
from rdkit.Chem import ChemicalFeatures
from rdkit.Chem import MolFromPDBBlock, MolFromSmiles

# Local modules
from luna.util.progress_tracker import ProgressTracker
from luna.mol.depiction import PharmacophoreDepiction
from luna.mol.clustering import cluster_fps_butina
from luna.mol.features import FeatureExtractor
from luna.mol.fingerprint import generate_fp_for_mols
from luna.mol.entry import MolEntry
from luna.mol.groups import AtomGroupPerceiver
from luna.mol.interaction.contact import get_contacts_for_entity
from luna.mol.interaction.calc import InteractionCalculator
from luna.mol.interaction.conf import InteractionConf
from luna.mol.interaction.fp.shell import ShellGenerator, IFPType
from luna.mol.wrappers.base import MolWrapper
from luna.mol.wrappers.rdkit import RDKIT_FORMATS, read_multimol_file
from luna.mol.amino_features import DEFAULT_AMINO_ATM_FEATURES
from luna.util.default_values import *
from luna.util.exceptions import *
from luna.util.file import pickle_data, unpickle_data, create_directory, get_file_format, get_unique_filename
from luna.util.logging import new_logging_file, load_default_logging_conf
import luna.util.logging_ini

from luna.MyBio.PDB.PDBParser import PDBParser
from luna.MyBio.selector import ResidueSelector
from luna.MyBio.util import download_pdb, entity_to_string, get_entity_from_entry
from luna.version import __version__


logger = logging.getLogger()


VERBOSITY_LEVEL = {4: logging.DEBUG,
                   3: logging.INFO,
                   2: logging.WARNING,
                   1: logging.ERROR,
                   0: logging.CRITICAL}


class EntryResults:

    def __init__(self, entry, atm_grps_mngr, interactions_mngr, ifp=None, mfp=None):

        self.entry = entry
        self.atm_grps_mngr = atm_grps_mngr
        self.interactions_mngr = interactions_mngr
        self.ifp = ifp
        self.mfp = mfp
        self.version = __version__

    def save(self, output_file, compressed=True):
        pickle_data(self, output_file, compressed)

    @staticmethod
    def load(input_file):
        return unpickle_data(input_file)


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

                 calc_mfp=False,
                 mfp_opts=None,
                 mfp_output=None,

                 calc_ifp=True,
                 ifp_num_levels=7,
                 ifp_radius_step=1,
                 ifp_length=IFP_LENGTH,
                 ifp_count=False,
                 ifp_diff_comp_classes=True,
                 ifp_type=IFPType.FIFP,
                 ifp_output=None,

                 similarity_func="BulkTanimotoSimilarity",
                 preload_mol_files=False,
                 default_properties=DEFAULT_AMINO_ATM_FEATURES,
                 butina_cutoff=0.2,
                 run_from_step=None,
                 run_until_step=None,

                 verbosity=3,
                 nproc=None):

        if verbosity not in VERBOSITY_LEVEL:
            logger.error("The informed logging level '%s' is not valid. The valid levels are: %s."
                         % (repr(verbosity), ", ".join(["%d (%s)" % (k, logging.getLevelName(v))
                                                        for k, v in sorted(VERBOSITY_LEVEL.items())])))
            sys.exit()
        else:
            logger.info("Verbosity set to: %d (%s)." % (verbosity, logging.getLevelName(verbosity)))

        if mol_obj_type not in ACCEPTED_MOL_OBJ_TYPES:
            logger.error("Invalid value for 'mol_obj_type'. Objects of type '%s' are not currently accepted. "
                         "The available options are: %s." % (mol_obj_type, ", ".join(["'%s'" % m for m in ACCEPTED_MOL_OBJ_TYPES])))
            sys.exit()

        if inter_conf is None:
            logger.info("No interaction configuration was set and the default will be used instead")
        elif inter_conf is not None and isinstance(inter_conf, InteractionConf) is False:
            logger.error("The informed interaction configuration must be an instance of %s."
                         % ".".join([InteractionConf.__module__, InteractionConf.__name__]))
            sys.exit()

        if inter_calc is not None and isinstance(inter_calc, InteractionCalculator) is False:
            logger.error("The informed interaction configuration must be an instance of %s."
                         % ".".join([InteractionCalculator.__module__, InteractionCalculator.__name__]))
            sys.exit()
        else:
            logger.info("No interaction calculator object was defined and the default will be used instead.")

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
        self.mfp_output = mfp_output

        self.calc_ifp = calc_ifp
        self.ifp_num_levels = ifp_num_levels
        self.ifp_radius_step = ifp_radius_step
        self.ifp_length = ifp_length
        self.ifp_count = ifp_count
        self.ifp_diff_comp_classes = ifp_diff_comp_classes
        self.ifp_type = ifp_type
        self.ifp_output = ifp_output

        self.similarity_func = similarity_func
        self.butina_cutoff = butina_cutoff
        self.default_properties = default_properties
        self.run_from_step = run_from_step
        self.run_until_step = run_until_step
        self.preload_mol_files = preload_mol_files
        self.step_controls = {}
        self.verbosity = VERBOSITY_LEVEL[verbosity]

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
        logger.info("The number of threads was set to: %d." % nproc)

        self.nproc = nproc

        self.version = __version__

        load_default_logging_conf()

        self.log_preferences()

    def __call__(self):
        raise NotImplementedError("This class is not callable. Use a class that implements this method.")

    @property
    def project_file(self):
        return "%s/project_v%s.pkl.gz" % (self.working_path, __version__)

    def run(self):
        self()

    def log_preferences(self):
        logger.debug("New project initialized...")
        params = ["\t\t\t-- %s = %s" % (key, str(self.__dict__[key])) for key in sorted(self.__dict__)]
        logger.debug("Preferences:\n%s" % "\n".join(params))

    def prepare_project_path(self):
        logger.info("Initializing project directory '%s'." % self.working_path)

        create_directory(self.working_path, self.overwrite_path)
        create_directory("%s/pdbs" % self.working_path)
        create_directory("%s/figures" % self.working_path)
        create_directory("%s/results/interactions" % self.working_path)
        create_directory("%s/results/fingerprints" % self.working_path)
        create_directory("%s/logs" % self.working_path)
        create_directory("%s/tmp" % self.working_path)
        create_directory("%s/chunks/" % self.working_path)

        logger.info("Project directory '%s' created successfully." % self.working_path)

    def init_logging_file(self, logging_filename=None, use_mp_handler=True):
        if not logging_filename:
            logging_filename = get_unique_filename(TMP_FILES)

        try:
            new_logging_file(logging_filename, logging_level=self.verbosity)

            logger.info("Logging file '%s' initialized successfully." % logging_filename)

            # Print preferences at the new logging file.
            self.log_preferences()
        except Exception as e:
            logger.exception(e)
            raise FileNotCreated("Logging file '%s' could not be created." % logging_filename)

    def remove_duplicate_entries(self):
        entries = {}
        for entry in self.entries:
            if entry.to_string() not in entries:
                entries[entry.to_string()] = entry
            else:
                logger.debug("An entry with id '%s' already exists in the list of entries, so the entry '%s' is a duplicate and will "
                             "be removed." % (entry.to_string(), entry))

        logger.info("The remotion of duplicate entries was finished. %d entrie(s) were removed." % (len(self.entries) - len(entries)))

        self.entries = list(entries.values())

    def validate_entry_format(self, entry):
        if not entry.is_valid():
            raise InvalidEntry("Entry '%s' does not match a LUNA's entry format." % entry.to_string())

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

    def decide_hydrogen_addition(self, pdb_header, entry):
        if self.try_h_addition:
            if "structure_method" in pdb_header:
                method = pdb_header["structure_method"]
                # If the method is not a NMR type does not add hydrogen as it usually already has hydrogens.
                if method.upper() in NMR_METHODS:
                    logger.debug("The structure related to the entry '%s' was obtained by NMR, so it will "
                                 "not add hydrogens to it." % entry)
                    return False
            return True
        return False

    def perceive_chemical_groups(self, entry, entity, ligand, add_h=False):
        logger.debug("Starting pharmacophore perception for entry '%s'" % entry.to_string())

        feature_factory = ChemicalFeatures.BuildFeatureFactory(self.atom_prop_file)
        feature_extractor = FeatureExtractor(feature_factory)

        perceiver = AtomGroupPerceiver(feature_extractor, add_h=add_h, ph=self.ph, amend_mol=self.amend_mol,
                                       mol_obj_type=self.mol_obj_type, default_properties=self.default_properties,
                                       tmp_path="%s/tmp" % self.working_path)

        radius = self.inter_conf.boundary_cutoff or BOUNDARY_CONF.boundary_cutoff
        nb_compounds = get_contacts_for_entity(entity, ligand, level='R', radius=radius)

        mol_objs_dict = {}
        if isinstance(entry, MolEntry):
            mol_objs_dict[entry.get_biopython_key()] = entry.mol_obj

        atm_grps_mngr = perceiver.perceive_atom_groups(set([x[1] for x in nb_compounds]), mol_objs_dict=mol_objs_dict)

        logger.debug("Pharmacophore perception for entry '%s' has finished." % entry.to_string())

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
                logger.debug("Molecular object in entry '%s' was manually informed and will not be reloaded." % entry.to_string())

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
                                logger.debug("A structure to the entry '%s' was found in the file '%s' and loaded "
                                             "into the entry." % (entry, mol_file))

                                # If there is no other molecules to search, just stop the loop.
                                if len(mol_files[key]) == 0:
                                    break
                        else:
                            logger.info("All target ligands located in the file '%s' were successfully loaded." % mol_file)
                    else:
                        targets = list(mol_files[key].keys())

                        for i, data in enumerate(read_multimol_file(mol_file, mol_format=ext, targets=targets, removeHs=False)):
                            rdk_mol = data[0]
                            mol_id = targets[i]
                            entry = mol_files[key][mol_id]
                            # It returns None if the molecule parsing generated errors.
                            if rdk_mol:
                                entry.mol_obj = rdk_mol
                                del(mol_files[key][mol_id])
                                logger.debug("A structure to the entry '%s' was found in the file '%s' and loaded "
                                             "into the entry." % (entry, mol_file))
                            else:
                                logger.debug("The structure related to the entry '%s' was found in the file '%s', but it could "
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
                logger.debug("Entry '%s' was not found or generated errors, so it will be removed "
                             "from the entries list." % entry)
            logger.warning("%d entrie(s) were removed during molecules loading due to errors or structure identification."
                           % len(invalid_entries))
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

        output = "%s/figures/%s.svg" % (self.working_path, rdmol.GetProp("_Name"))

        # TODO: Adapt it to use the PharmacophoreDepiction

        # ligand_pharm_figure(rdmol, atm_types, output, ATOM_TYPES_COLOR)

    def create_mfp(self, entry):
        if isinstance(entry, MolEntry):
            rdmol_lig = MolFromSmiles(MolWrapper(entry.mol_obj).to_smiles())
            rdmol_lig.SetProp("_Name", entry.mol_id)

            return generate_fp_for_mols([rdmol_lig], "morgan_fp")[0]["fp"]
        else:
            logger.warning("Currently, it cannot generate molecular fingerprints for "
                           "instances of %s." % entry.__class__.__name__)

    def create_ifp(self, atm_grps_mngr):
        sg = ShellGenerator(self.ifp_num_levels, self.ifp_radius_step,
                            diff_comp_classes=self.ifp_diff_comp_classes,
                            ifp_type=self.ifp_type)
        sm = sg.create_shells(atm_grps_mngr)

        unique_shells = not self.ifp_count
        return sm.to_fingerprint(fold_to_size=self.ifp_length, unique_shells=unique_shells, count_fp=self.ifp_count)

    def create_ifp_file(self):
        self.ifp_output = self.ifp_output or "%s/results/fingerprints/ifp.csv" % self.working_path
        with open(self.ifp_output, "w") as OUT:
            if self.ifp_count:
                OUT.write("ligand_id,smiles,on_bits,count\n")
            else:
                OUT.write("ligand_id,smiles,on_bits\n")

            for entry, ifp in self.ifps:
                if self.ifp_count:
                    fp_bits_str = "\t".join([str(idx) for idx in ifp.counts.keys()])
                    fp_count_str = "\t".join([str(count) for count in ifp.counts.values()])
                    OUT.write("%s,%s,%s,%s\n" % (entry.to_string(), "", fp_bits_str, fp_count_str))
                else:
                    fp_bits_str = "\t".join([str(x) for x in ifp.get_on_bits()])
                    OUT.write("%s,%s,%s\n" % (entry.to_string(), "", fp_bits_str))

    def create_mfp_file(self):
        self.mfp_output = self.mfp_output or "%s/results/fingerprints/mfp.csv" % self.working_path
        with open(self.mfp_output, "w") as OUT:
            OUT.write("ligand_id,smiles,on_bits\n")
            for entry, mfp in self.mfps:
                fp_str = "\t".join([str(x) for x in mfp.GetOnBits()])
                OUT.write("%s,%s,%s\n" % (entry.to_string(), "", fp_str))

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
        logger.info("Reloading project saved in '%s'" % input_file)
        proj_obj = unpickle_data(input_file)
        proj_obj.init_logging_file("%s/logs/project.log" % proj_obj.working_path)

        return proj_obj


class LocalProject(Project):

    def __init__(self, entries, working_path, has_local_files=False, **kwargs):
        super().__init__(entries=entries, working_path=working_path, has_local_files=has_local_files, **kwargs)

    @property
    def results(self):
        for entry in self.entries:
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            yield EntryResults.load(pkl_file)

    @property
    def interaction_mngrs(self):
        for entry in self.entries:
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            yield EntryResults.load(pkl_file).interactions_mngr

    @property
    def atm_grps_mngrs(self):
        for entry in self.entries:
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            yield EntryResults.load(pkl_file).atm_grps_mngr

    @property
    def ifps(self):
        for entry in self.entries:
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            yield entry, EntryResults.load(pkl_file).ifp

    @property
    def mfps(self):
        for entry in self.entries:
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            yield entry, EntryResults.load(pkl_file).mfp

    def _producer(self, queue):
        for entry in self.entries:
            queue.put(entry)

    def _consumer(self, func, queue, progress_tracker):
        while True:
            start = time.time()
            entry = queue.get()

            # Run the provided function
            func(entry, progress_tracker)

            progress_tracker.progress += 1

            end = time.time()
            progress_tracker.running_times.append((end - start))

            queue.task_done()

    def _process_entry(self, entry, progress_tracker):
        try:
            logger.debug("Starting entry processing: %s." % entry.to_string())

            # Check if the entry is in the correct format.
            # It also accepts entries whose pdb_id is defined by the filename.
            if isinstance(entry, MolEntry) is False:
                self.validate_entry_format(entry)

            # TODO: allow the user to pass a pdb_file through entries.
            pdb_file = self.get_pdb_file(entry.pdb_id)
            entry.pdb_file = pdb_file

            pdb_parser = PDBParser(PERMISSIVE=True, QUIET=True, FIX_ATOM_NAME_CONFLICT=True, FIX_OBABEL_FLAGS=False)
            structure = pdb_parser.get_structure(entry.pdb_id, pdb_file)
            add_hydrogen = self.decide_hydrogen_addition(pdb_parser.get_header(), entry)

            if isinstance(entry, MolEntry):
                structure = entry.get_biopython_structure(structure, pdb_parser)

            ligand = get_entity_from_entry(structure, entry)
            ligand.set_as_target(is_target=True)

            #
            # Perceive pharmacophoric properties
            #
            atm_grps_mngr = self.perceive_chemical_groups(entry, structure[0], ligand, add_hydrogen)
            atm_grps_mngr.entry = entry

            #
            # Calculate interactions
            #
            interactions_mngr = self.inter_calc.calc_interactions(atm_grps_mngr.atm_grps)
            interactions_mngr.entry = entry

            # Create hydrophobic islands.
            atm_grps_mngr.merge_hydrophobic_atoms(interactions_mngr)

            # Generate IFP (Interaction fingerprint)
            ifp = None
            if self.calc_ifp:
                ifp = self.create_ifp(atm_grps_mngr)

            # Generate MFP (Molecular fingerprint)
            mfp = None
            if self.calc_mfp:
                mfp = self.create_mfp()

            entry_results = EntryResults(entry, atm_grps_mngr, interactions_mngr, ifp, mfp)

            # Saving entry results.
            pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())
            entry_results.save(pkl_file)

            # Saving interactions to CSV file.
            csv_file = "%s/results/interactions/%s.csv" % (self.working_path, entry.to_string())
            interactions_mngr.to_csv(csv_file)

            # Delete molecular objects to save memory.
            if isinstance(entry, MolEntry):
                entry.mol_obj = None

            logger.debug("Processing of entry '%s' finished successfully." % entry.to_string())
        except Exception as e:
            logger.exception(e)
            logger.debug("Processing of entry %s failed. Check the logs for more information." % entry.to_string())

            progress_tracker.errors.append(entry)

    def _process_ifps(self, entry, progress_tracker):
        pkl_file = "%s/chunks/%s.pkl.gz" % (self.working_path, entry.to_string())

        if exists(pkl_file):
            # Reload results.
            entry_results = EntryResults.load(pkl_file)
            atm_grps_mngr = entry_results.atm_grps_mngr

            # Generate a new IFP.
            ifp = self.create_ifp(atm_grps_mngr)

            # Substitute old IFP by the new version and save the project.
            entry_results.ifp = ifp
            entry_results.save(pkl_file)
        else:
            logger.debug("The IFP for the entry %s cannot be generated because its pickled "
                         "data file '%s' was not found." % (entry, pkl_file))
            progress_tracker.errors.append(entry)

    def __call__(self):
        start = time.time()

        self.prepare_project_path()
        self.init_logging_file("%s/logs/project.log" % self.working_path)

        self.remove_duplicate_entries()

        if self.preload_mol_files:
            self.add_mol_obj_to_entries()

        job_queue = Queue(maxsize=self.nproc)

        logger.info("Entries processing will start. Number of entries to be processed is: %d." % len(self.entries))

        entry_processing = ProgressTracker(len(self.entries), "Entries processing.")
        entry_processing.start()

        for i in range(self.nproc):
            t = Thread(name="ConsumerThread-%d" % i, target=self._consumer, args=(self._process_entry, job_queue, entry_processing))
            t.daemon = True
            t.start()

        # One producer thread.
        t = Thread(name="ProducerThread", target=self._producer, args=(job_queue,))
        t.start()

        job_queue.join()
        entry_processing.end()

        logger.info("Entries processing finished successfully.")

        # Generate IFP/MFP files
        if self.calc_ifp:
            self.create_ifp_file()
        if self.calc_mfp:
            self.create_mfp_file()

        # Save the whole project information.
        self.save(self.project_file)

        end = time.time()
        logger.info("Project creation completed!!!")
        logger.info("Total processing time: %.2fs." % (end - start))
        logger.info("Results were saved at %s." % self.working_path)
        logger.info("You can reload your project from %s." % self.project_file)
        print()
        print()

    def generate_ifps(self):
        start = time.time()

        self.calc_ifp = True

        job_queue = Queue(maxsize=self.nproc)

        entry_processing = ProgressTracker(len(self.entries), "IFPs generation.")
        entry_processing.start()

        for i in range(self.nproc):
            t = Thread(name="ConsumerThread-%d" % i, target=self._consumer, args=(self._process_ifps, job_queue, entry_processing))
            t.daemon = True
            t.start()

        # One producer thread.
        t = Thread(name="ProducerThread", target=self._producer, args=(job_queue,))
        t.start()

        job_queue.join()

        entry_processing.end()

        # Generate IFP/MFP files
        if self.calc_ifp:
            self.create_ifp_file()
        if self.calc_mfp:
            self.create_mfp_file()

        end = time.time()
        logger.info("IFPs created successfully!!!")
        logger.info("Total processing time: %.2fs." % (end - start))
        logger.info("Results were saved at %s." % self.working_path)
