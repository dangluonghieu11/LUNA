from util.default_values import *
from util.exceptions import *
from util.file import (create_directory, is_file_valid, get_file_format, get_filename, get_unique_filename)
from util.logging import new_logging_file
from util.config_parser import Config
from util.function import func_call_to_str

from MyBio.util import (download_pdb, entity_to_string)
from MyBio.selector import ResidueSelector

from rdkit.Chem import ChemicalFeatures
from rdkit.Chem.Pharm2D.SigFactory import SigFactory
from rdkit.Chem import (MolFromPDBBlock, MolFromSmiles)
from rdkit.Chem.rdDepictor import Compute2DCoords

from pybel import (informats, readfile)

# Get nearby molecules (contacts)
from mol.interaction.contact import get_contacts_for_entity
from mol.interaction.calc_interactions import (calc_all_interactions, apply_interaction_criteria)

from mol.interaction.calc import InteractionCalculator
from mol.interaction.conf import InteractionConf
from mol.interaction.filter import InteractionFilter

from MyBio.PDB.PDBParser import PDBParser

from mol.entry import (DBEntry, MolEntry, PLIEntryValidator)
from mol.groups import CompoundGroupPerceiver
from mol.fingerprint import generate_fp_for_mols
from mol.features import FeatureExtractor
from mol.wrappers.obabel import convert_molecule
from mol.clustering import cluster_fps_butina
from mol.depiction import ligand_pharm_figure
from mol.interaction.fp.shell import (ShellGenerator, Fingerprint, CountFingerprint)

from analysis.residues import (InteractingResidues, get_interacting_residues)
from analysis.summary import *

from sqlalchemy.orm.exc import NoResultFound

from database.loader import *
from database.napoli_model import *
from database.helpers import *
from database.util import (get_ligand_tbl_join_filter, default_interaction_filters,
                           format_db_ligand_entries, format_db_interactions,
                           object_as_dict, get_default_mappers_list)

from math import ceil
from os.path import exists
from collections import defaultdict
from functools import wraps
import time
import multiprocessing as mp


PDB_PARSER = PDBParser(PERMISSIVE=True, QUIET=True, FIX_ATOM_NAME_CONFLICT=False, FIX_OBABEL_FLAGS=False)


def iter_to_chunks(l, n):
    return [l[i:i + n] for i in range(0, len(l), n)]


class StepControl:

    def __init__(self, step_id, num_subtasks, num_executed_subtasks,
                 is_complete=False):

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
        return '<StepControl: [Step id=%d, Progress=%.2f%%]>' % (self.step_id,
                                                                 self.progress)


class ExceptionWrapper(object):

    def __init__(self, step_id, has_subtasks, is_critical, desc=None):
        self.step_id = step_id
        self.has_subtasks = has_subtasks
        self.is_critical = is_critical
        self.desc = desc

    def __call__(self, func):
        @wraps(func)
        def callable(*args, **kwargs):
            # InteractionsProject class
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

                proj_obj.logger.warning("The function '%s' finished "
                                        "successfully." % func.__name__)

                return result
            except Exception as e:
                proj_obj.logger.warning("The function '%s' failed."
                                        % func.__name__)
                proj_obj.logger.exception(e)

                # TODO: se chegar uma mensagem de erro não user friendly,
                # coloque uma mensagem generica
                error_message = e.args[0]
                if self.is_critical:
                    proj_obj.logger.warning("As the called function was "
                                            "critical, the program "
                                            "will be aborted.")
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


class InteractionsProject:

    def __init__(self,
                 entries,
                 working_path,
                 pdb_path=PDB_PATH,
                 has_local_files=False,
                 overwrite_path=False,
                 db_conf_file=None,
                 pdb_template=None,
                 atom_prop_file=ATOM_PROP_FILE,
                 interaction_conf=INTERACTION_CONF,
                 ph=7.4,
                 add_hydrogen=True,
                 add_non_cov=True,
                 add_atom_atom=True,
                 add_proximal=False,
                 calc_mfp=True,
                 mfp_func="pharm2d_fp",
                 calc_ifp=True,
                 ifp_num_levels=10,
                 ifp_radius_step=1,
                 ifp_length=IFP_LENGTH,
                 similarity_func="BulkTanimotoSimilarity",
                 preload_mol_files=False,
                 butina_cutoff=0.2,
                 run_from_step=None,
                 run_until_step=None):

        self.entries = entries
        self.working_path = working_path
        self.pdb_path = pdb_path
        self.has_local_files = has_local_files
        self.overwrite_path = overwrite_path
        self.db_conf_file = db_conf_file
        self.pdb_template = pdb_template
        self.atom_prop_file = atom_prop_file
        self.interaction_conf = interaction_conf
        self.ph = ph
        self.add_hydrogen = add_hydrogen
        self.add_non_cov = add_non_cov
        self.add_atom_atom = add_atom_atom
        self.add_proximal = add_proximal

        self.calc_mfp = calc_mfp
        self.mfp_func = mfp_func
        self.calc_ifp = calc_ifp
        self.ifp_num_levels = ifp_num_levels
        self.ifp_radius_step = ifp_radius_step
        self.ifp_length = ifp_length

        self.similarity_func = similarity_func
        self.butina_cutoff = butina_cutoff
        self.run_from_step = run_from_step
        self.run_until_step = run_until_step
        self.preload_mol_files = preload_mol_files

        self.step_controls = {}

    def __call__(self):
        raise NotImplementedError("This class is not callable. The function __call__() is an abstract method.")

    def init_logging_file(self, logging_filename=None):
        if not logging_filename:
            logging_filename = get_unique_filename(TMP_FILES)

        logger = new_logging_file(logging_filename)
        if logger:
            self.logger = logger
            self.logger.warning("Logging file initialized successfully.")
        else:
            raise FileNotCreated("Logging file could not be created.")

    def init_db_connection(self):
        logger.info("A database configuration file was defined. "
                    "Starting a new database connection...")

        config = Config(self.db_conf_file)
        dbConf = config.get_section_map("database")
        self.db = DBLoader(**dbConf)

    def init_common_tables(self):
        for conf in get_default_mappers_list(self.db):
            self.db.new_mapper(conf.table_class,
                               conf.table_name,
                               conf.properties)

    def get_or_create_task(self, step_id, has_subtasks):
        if step_id in self.step_controls:
            task = self.step_controls[step_id]
        else:
            num_subtasks = len(self.entries) if has_subtasks else 0
            num_executed_subtasks = 0
            task = StepControl(step_id, num_subtasks, num_executed_subtasks)
        return task

    def get_status_id(self, status_name):
        if status_name not in self.status_control:
            raise KeyError("The Status table does not have an %s entry for project control." % status_name)

        return self.status_control[status_name].id

    def update_step_details(self, task, error_message=None):
        status_id = self.get_status_id("RUNNING")
        warning = None
        if task.has_subtasks:
            if task.is_complete:
                status_id = self.get_status_id("DONE")

            if error_message:
                warning = "One or more entries failed."
                db_message = ProjectStepMessage(self.project_id,
                                                task.step_id,
                                                self.get_status_id("WARNING"),
                                                error_message)
                self.db.session.add(db_message)

                db_ligand_entry = (self.db.session
                                   .query(LigandEntry)
                                   .filter(LigandEntry.id == self.current_entry.id)
                                   .first())

                db_ligand_entry.step_messages.append(db_message)
        else:
            if task.is_complete:
                if error_message:
                    status_id = self.get_status_id("FAILED")
                else:
                    status_id = self.get_status_id("DONE")

        # TODO: verificar se temos um banco de dados para ser atualizado ou não
        db_step = ProjectStepDetail(self.project_id, task.step_id,
                                    status_id, warning, task.progress)

        self.db.session.merge(db_step)
        self.db.approve_session()

    # @ExceptionWrapper(step_id=1, has_subtasks=False, is_critical=True)
    def prepare_project_path(self):
        create_directory(self.working_path, self.overwrite_path)
        create_directory("%s/pdbs" % self.working_path)
        create_directory("%s/figures" % self.working_path)
        create_directory("%s/results" % self.working_path)
        create_directory("%s/tmp" % self.working_path)

    def validate_entry_format(self, ligand_entry):
        entry_str = ligand_entry.to_string(ENTRIES_SEPARATOR)
        if not self.entry_validator.is_valid(entry_str):
            raise InvalidNapoliEntry("Entry '%s' does not match the nAPOLI entry format." % entry_str)

    def get_pdb_file(self, pdb_id):
        pdb_file = "%s/%s.pdb" % (self.pdb_path, pdb_id)

        if self.has_local_files:
            if not exists(pdb_file):
                raise FileNotFoundError("The PDB file '%s' was not found." % pdb_file)
        elif not exists(pdb_file):
            working_pdb_path = "%s/pdbs" % self.working_path
            pdb_file = "%s/%s.pdb" % (working_pdb_path, pdb_id)

            try:
                download_pdb(pdb_id=pdb_id, output_path=working_pdb_path, output_file=pdb_file)
            except Exception as e:
                logger.exception(e)
                raise FileNotCreated("PDB file '%s' was not created." % pdb_file) from e

        return pdb_file

    def decide_hydrogen_addition(self, pdb_header):
        if self.add_hydrogen:
            if "structure_method" in pdb_header:
                method = pdb_header["structure_method"]
                # If the method is not a NMR type, which, in general, already have hydrogens.
                if method.upper() in NMR_METHODS:
                    return False
            return True
        return False

    def add_hydrogen(self, pdb_file):
        new_pdb_file = pdb_file

        if self.add_hydrogen:
            pdb_id = get_filename(pdb_file)
            new_pdb_file = "%s/pdbs/%s.H.pdb" % (self.working_path, pdb_id)

            if not exists(new_pdb_file):
                run_obabel = False
                # if self.has_local_files:
                #     run_obabel = True
                # else:
                #     db_structure = (self.db.session
                #                     .query(Structure)
                #                     .filter(Structure.pdb_id == pdb_id)
                #                     .first())

                #     if db_structure:
                #         # If the method is not a NMR type, which,
                #         # in general, already have hydrogens
                #         if db_structure.experimental_tech.name != "NMR":
                #             run_obabel = True
                #     else:
                #         try:
                #             PDB_PARSER.get_structure(pdb_id, pdb_file)
                #             if PDB_PARSER.get_header():
                #                 if "structure_method" in PDB_PARSER.get_header():
                #                     method = (PDB_PARSER.get_header()["structure_method"])
                #                     # If the method is not a NMR type, which,
                #                     # in general, already have hydrogens
                #                     if method.upper() not in NMR_METHODS:
                #                         run_obabel = True
                #             else:
                #                 run_obabel = True
                #         except Exception:
                #             run_obabel = True

                # TODO: REMOVE
                run_obabel = True
                if run_obabel:
                    # First, it removes all hydrogen atoms.
                    ob_opt = {"d": None, "error-level": 5}
                    convert_molecule(pdb_file, new_pdb_file, opt=ob_opt)

                    # Now, it adds hydrogen atoms.
                    if self.ph:
                        ob_opt = {"p": 7, "error-level": 5}
                    else:
                        ob_opt = {"h": None, "error-level": 5}
                    convert_molecule(pdb_file, new_pdb_file, opt=ob_opt)

        return new_pdb_file

    def get_target_entity(self, entity, entry, model=0):
        structure = entity.get_parent_by_level("S")
        model = structure[model]

        if entry.chain_id in model.child_dict:
            chain = model[entry.chain_id]
            if entry.is_hetatm():
                ligand_key = entry.get_biopython_key()
                if ligand_key in chain.child_dict:
                    target_entity = chain[ligand_key]
                else:
                    raise MoleculeNotFoundError("Ligand '%s' does not exist in the PDB '%s'."
                                                % (entry.to_string(ENTRIES_SEPARATOR),
                                                   structure.get_id()))
            else:
                target_entity = chain
        else:
            raise ChainNotFoundError("The informed chain id '%s' for the ligand entry '%s' does not exist "
                                     "in the PDB '%s'."
                                     % (entry.chain_id, entry.to_string(ENTRIES_SEPARATOR), structure.get_id()))

        return target_entity

    def perceive_chemical_groups(self, entity, ligand, add_h=False):
        perceiver = CompoundGroupPerceiver(self.feature_extractor, add_h=add_h, ph=self.ph,
                                           tmp_path="%s/tmp" % self.working_path)

        nb_compounds = get_contacts_for_entity(entity, ligand, level='R')

        grps_by_compounds = {}
        for comp in set([x[1] for x in nb_compounds]):
            # TODO: verificar se estou usando o ICODE nos residuos
            if comp.id == self.current_entry.get_biopython_key() and isinstance(self.current_entry, MolEntry):
                groups = perceiver.perceive_compound_groups(comp, self.current_entry.mol_obj)
            else:
                groups = perceiver.perceive_compound_groups(comp)
            grps_by_compounds[comp] = groups

        logger.info("Chemical group perception finished!!!")

        return grps_by_compounds

    def set_pharm_objects(self):
        feature_factory = ChemicalFeatures.BuildFeatureFactory(self.atom_prop_file)

        sig_factory = SigFactory(feature_factory, minPointCount=2, maxPointCount=3, trianglePruneBins=False)
        sig_factory.SetBins([(0, 2), (2, 5), (5, 8)])
        sig_factory.Init()
        self.sig_factory = sig_factory

        self.feature_extractor = FeatureExtractor(feature_factory)

    def get_fingerprint(self, rdmol):
        fp_opt = {"sigFactory": self.sig_factory}
        fp = generate_fp_for_mols([rdmol], self.mfp_func,
                                  fp_opt=fp_opt, critical=True)[0]

        return fp

    def recover_rcsb_interactions(self, ligand_entry, manager):
        entry_str = ligand_entry.to_string(ENTRIES_SEPARATOR)

        logger.info("Trying to select pre-computed interactions for "
                    "the entry '%s'." % entry_str)

        db_ligand_entity = (self.db.session
                            .query(Ligand)
                            .filter(Ligand.pdb_id == ligand_entry.pdb_id and
                                    Ligand.chain_id == ligand_entry.chain_id and
                                    Ligand.lig_name == ligand_entry.lig_name and
                                    Ligand.lig_num == ligand_entry.lig_num and
                                    Ligand.lig_icode == ligand_entry.lig_icode)
                            .first())

        db_interactions = None
        if db_ligand_entity:
            # TODO: Mapear ligand_entity com status
            if db_ligand_entity.status.title == "AVAILABLE":
                join_filter = get_ligand_tbl_join_filter(ligand_entry, Ligand)
                db_interactions = (manager.select_interactions(join_filter,
                                                               interFilters))

                logger.info("%d pre-computed interaction(s) found in the "
                            "database for the entry '%s'."
                            % (len(filtered_inter), entry_str))
            else:
                logger.info("The entry '%s' exists in the database, but "
                            "there is no pre-computed interaction available. "
                            "So, nAPOLI will calculate the interactions to "
                            "this ligand." % entry_str)
        else:
            logger.info("The entry '%s' does not exist in the "
                        "database." % entry_str)

        return db_interactions

    def set_step_details(self):
        db_proj_type = (self.db.session.query(ProjectType)
                        .filter(ProjectType.type == self.project_type).first())

        if not db_proj_type:
            raise NoResultFound("No step details found for the project "
                                "type '%s'." % self.job_code)

        step_details = {}
        for db_step in db_proj_type.steps:
            step_details[db_step.id] = object_as_dict(db_step)

        self.step_details = step_details

    # @ExceptionWrapper(step_id=1, has_subtasks=False, is_critical=True)
    def set_project_id(self):
        db_proj = (self.db.session
                   .query(Project)
                   .filter(Project.job_code == self.job_code).one_or_none())

        if not db_proj:
            raise NoResultFound("Job code '%s' does not exist in the database."
                                % self.job_code)
        else:
            self.project_id = db_proj.id

    def recover_all_entries(self):
        db_ligand_entries = (LigandEntryManager(self.db)
                             .get_entries(self.project_id))

        if not db_ligand_entries:
            raise NoResultFound("No ligand entries for the informed job code "
                                "'%s' was found." % self.job_code)

        return db_ligand_entries

    def set_status_control(self):
        db_status_entries = (self.db.session.query(Status).all())

        if not db_status_entries:
            raise NoResultFound("No status entries was found.")

        self.status_control = {r.name: r for r in db_status_entries}

    def get_rdkit_mol(self, entity, target, mol_name="Mol0"):
        target_sel = ResidueSelector({target})
        pdb_block = entity_to_string(entity, target_sel, write_conects=False)
        rdmol = MolFromPDBBlock(pdb_block)
        rdmol.SetProp("_Name", mol_name)

        return rdmol

    def add_mol_obj_to_entries(self):
        mol_files = defaultdict(dict)
        for entry in self.entries:
            mol_files[entry.mol_file][entry.mol_id] = entry

        for mol_file in mol_files:
            ext = get_file_format(mol_file)
            if ext not in informats:
                raise IllegalArgumentError("Extension '%s' informed or assumed by the filename is not a "
                                           "recognised Open Babel format." % ext)
            if not exists(mol_file):
                raise FileNotFoundError("The file '%s' was not found." % mol_file)

            try:
                for ob_mol in readfile(ext, mol_file):
                    if ob_mol.OBMol.GetTitle() in mol_files[mol_file]:
                        logger.info("Loading molecule object to ligand %s." % ob_mol.OBMol.GetTitle())
                        entry = mol_files[mol_file][ob_mol.OBMol.GetTitle()]
                        entry.mol_obj = ob_mol
                        del(mol_files[mol_file][ob_mol.OBMol.GetTitle()])
                    # If there is no other molecules to search, just stop the loop.
                    if len(mol_files[mol_file]) == 0:
                        break
            except Exception as e:
                logger.exception(e)
                raise MoleculeObjectError("An error occurred while parsing the file with Open Babel and "
                                          "the molecule object could not be created. Check the logs for "
                                          "more information.")

        invalid_entries = [e for m in mol_files for e in mol_files[m].values()]
        if invalid_entries:
            entries = set(self.entries)
            for entry in invalid_entries:
                entries.remove(entry)
                logger.warning("Ligand in entry '%s' not found in the informed file '%s'. It will be removed."
                               % (entry, entry.mol_file))
            logger.warning("%d invalid entrie(s) removed." % len(invalid_entries))
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
            clusters = cluster_fps_butina(fps_only,
                                          cutoff=self.butina_cutoff,
                                          similarity_func=self.similarity_func)
        except Exception:
            raise ProcessingFailed("Clustering step failed.")

        lig_clusters = {}
        for i, cluster in enumerate(clusters):
            for mol_id in cluster:
                lig_clusters[fingerprints[mol_id]["mol"]] = i

        return lig_clusters

    def store_compound_statistics(self, lig_entry_id, grp_types_count):
        for type_id in grp_types_count:
            self.db.session.merge(CompTypeCount(lig_entry_id,
                                                self.project_id,
                                                type_id,
                                                grp_types_count[type_id]))
        # TODO: Remover comentario
        # self.db.approve_session()

    def store_interaction_statistics(self, lig_entry_id, inter_type_count):
        for type_id in inter_type_count:
            self.db.session.merge(InterTypeCount(lig_entry_id,
                                                 self.project_id,
                                                 type_id,
                                                 inter_type_count[type_id]))
        # TODO: Remover comentario
        # self.db.approve_session()

    def update_freq_by_cluster(self, count_dict, cluster_id, freq_by_cluster):
        for type_id in count_dict:
            key = (cluster_id, type_id, count_dict[type_id])
            freq_by_cluster[key] += 1

    def store_inter_type_freq(self, inter_type_freq_by_cluster):
        cumulative_num = defaultdict(int)
        keys = sorted(inter_type_freq_by_cluster)
        for key in keys:
            (cluster_id, inter_type_id, count) = key
            freq = inter_type_freq_by_cluster[key]
            cumulative_num[(cluster_id, inter_type_id)] += freq
            db_freq = InterTypeFreqByCluster(self.project_id, inter_type_id,
                                             cluster_id, count, freq,
                                             cumulative_num[(cluster_id, inter_type_id)])
            self.db.session.merge(db_freq)
        # TODO: Remover comentario
        # self.db.approve_session()

    def store_comp_type_freq(self, comp_type_freq_by_cluster):
        cumulative_num = defaultdict(int)
        keys = sorted(comp_type_freq_by_cluster)
        for key in keys:
            (cluster_id, comp_type_id, count) = key
            freq = comp_type_freq_by_cluster[key]
            cumulative_num[(cluster_id, comp_type_id)] += freq
            db_freq = CompTypeFreqByCluster(self.project_id, comp_type_id,
                                            cluster_id, count, freq,
                                            cumulative_num[(cluster_id, comp_type_id)])
            self.db.session.merge(db_freq)
        # TODO: Remover comentario
        # self.db.approve_session()


# POPULATE RCSB.
class RCSB_PLI_Population(InteractionsProject):
    def __init__(self, entries, working_path, db_conf_file, **kwargs):

        super().__init__(entries=entries, working_path=working_path, db_conf_file=db_conf_file,
                         has_local_files=False, **kwargs)

    def __call__(self):
        self.init_logging_file()
        self.init_db_connection()

        self.prepare_project_path()
        self.init_common_tables()

        self.set_pharm_objects()

        self.entry_validator = PLIEntryValidator()
        rcsb_inter_manager = RCSBInteractionManager(self.db)

        # Loop over each entry.
        for ligand_entry in self.entries:
            mybio_ligand = ("H_%s" % ligand_entry.lig_name,
                            ligand_entry.lig_num,
                            ligand_entry.lig_icode)

            self.check_entry_existance(ligand_entry)
            self.validate_entry_format(ligand_entry)

            pdb_file = self.get_pdb_file(ligand_entry.pdb_id)
            pdb_file = self.add_hydrogen(pdb_file)

            structure = PDB_PARSER.get_structure(ligand_entry.pdb_id, pdb_file)
            ligand = structure[0][ligand_entry.chain_id][mybio_ligand]

            grps_by_compounds = self.perceive_chemical_groups(structure[0], ligand)
            src_grps = [grps_by_compounds[x] for x in grps_by_compounds
                        if x.get_id()[0] != " "]
            trgt_grps = [grps_by_compounds[x] for x in grps_by_compounds
                         if x != ligand]

            all_inter = calc_all_interactions(src_grps, trgt_grps, conf=BOUNDARY_CONF)

            rcsb_inter_manager.insert_interactions(all_inter, db_ligand_entity)

    def check_entry_existance(self, ligand_entry):
        db_ligand_entity = (self.db.session
                            .query(Ligand)
                            .filter(Ligand.pdb_id == ligand_entry.pdb_id and
                                    Ligand.chain_id == ligand_entry.chain_id and
                                    Ligand.lig_name == ligand_entry.lig_name and
                                    Ligand.lig_num == ligand_entry.lig_num and
                                    Ligand.lig_icode == ligand_entry.lig_icode)
                            .first())

        # If this entry does not exist in the database,
        # raise an error.
        if db_ligand_entity is None:
            message = ("Entry '%s' does not exist in the table 'ligand'." % ligand_entry.to_string(ENTRIES_SEPARATOR))
            raise InvalidNapoliEntry(message)

        # If there are already interactions to this entry,
        # raise an error.
        elif status_by_id[db_ligand_entity.status_id] == "AVAILABLE":
            raise DuplicateEntry("Interactions to the entry '%s' already exists in the database."
                                 % ligand_entry.to_string(ENTRIES_SEPARATOR))


class DB_PLI_Project(InteractionsProject):

    def __init__(self, db_conf_file, job_code, working_path=None, entries=None, keep_all_potential_inter=True,
                 has_local_files=False, **kwargs):

        self.job_code = job_code

        if not working_path:
            working_path = "%s/projects/%s" % (NAPOLI_PATH, job_code)

        # If entries were not informed, get it from the database.
        self.get_entries_from_db = (entries is None)

        self.keep_all_potential_inter = keep_all_potential_inter

        self.project_type = "PLI analysis"
        self.has_local_files = has_local_files

        super().__init__(db_conf_file=db_conf_file, working_path=working_path, entries=entries, **kwargs)

    def __call__(self):

        # TODO: verificar o numero de entradas

        # TODO: Definir as mensagens de descrição para o usuário
        # TODO: colocar wrapers em cada função

        # TODO: add a new parameter: hydrogen addition mode to define how hydrogens will be added OR:
        #       add a new parameter: force hydrogen addition.

        self.init_logging_file()
        self.init_db_connection()
        self.init_common_tables()

        self.set_project_id()
        self.set_step_details()
        self.set_status_control()

        # Recover and set the entries related to this project
        if self.get_entries_from_db:
            db_lig_entries = self.recover_all_entries()
            db_lig_entries_by_id = {x.id: x for x in db_lig_entries}
            self.entries = format_db_ligand_entries(db_lig_entries)

        self.prepare_project_path()

        self.set_pharm_objects()

        free_filename_format = self.has_local_files
        self.entry_validator = PLIEntryValidator(free_filename_format)

        rcsb_inter_manager = RCSBInteractionManager(self.db)
        proj_inter_manager = ProjectInteractionManager(self.db)

        db_comp_types = self.db.session.query(CompoundType).all()
        comp_type_id_map = {r.type: r.id for r in db_comp_types}
        db_inter_types = self.db.session.query(InterType).all()
        inter_type_id_map = {r.type: r.id for r in db_inter_types}

        # The calculus of interactions depend on the pharmacophore definition (ATOM_PROP_FILE) and the pH (PH).
        # Thus, if the user has kept such values unchanged and has not uploaded any PDB file, nAPOLI can try to
        # select the pre-computed interactions from a database.
        is_inter_params_default = (self.pdb_path is PDB_PATH and
                                   self.atom_prop_file == ATOM_PROP_FILE and
                                   self.ph == 7 and self.db_conf_file is not None)
        if is_inter_params_default:
            logger.info("Default configuration was kept unchanged. nAPOLI will try to select pre-computed "
                        "interactions from the defined database")

        fingerprints = []

        comp_type_count_by_entry = {}
        inter_type_count_by_entry = {}
        inter_res_by_entry = {}

        # Loop over each entry.
        for ligand_entry in self.entries:
            self.current_entry = ligand_entry

            # User has informed entries manually.
            # Check if the entries exist in the database.
            if self.get_entries_from_db is False:
                db_ligand_entity = self.recover_ligand_entry(ligand_entry)
            else:
                db_ligand_entity = db_lig_entries_by_id[ligand_entry.id]

            # Check if the entry is in the correct format.
            # It also accepts entries whose pdb_id is defined by
            # the filename.
            self.validate_entry_format(ligand_entry)

            pdb_file = self.get_pdb_file(ligand_entry.pdb_id)

            # TODO: resolver o problema dos hidrogenios.
            #       Vou adicionar hidrogenio a pH 7?
            #       Usuario vai poder definir pH?
            #       Obabel gera erro com posicoes alternativas

            structure = PDB_PARSER.get_structure(ligand_entry.pdb_id, pdb_file)
            ligand = self.get_target_entity(structure, ligand_entry)

            # to_add_hydrogen = self.decide_hydrogen_addition(PDB_PARSER.get_header())
            # print(to_add_hydrogen)
            # exit()

            # exit()

            grps_by_compounds = self.perceive_chemical_groups(structure[0], ligand)
            src_grps = [grps_by_compounds[x] for x in grps_by_compounds if x.get_id()[0] != " "]
            trgt_grps = [grps_by_compounds[x] for x in grps_by_compounds if x != ligand]

            calc_interactions = True
            if is_inter_params_default:
                db_interactions = self.recover_rcsb_interactions(ligand_entry,
                                                                 rcsb_inter_manager)

                if db_interactions is not None:
                    filtered_inter = format_db_interactions(structure,
                                                            db_interactions)
                    calc_interactions = False

            if calc_interactions:
                if self.keep_all_potential_inter:
                    # TODO: calcular ponte de hidrogenio fraca
                    # TODO: detectar interações covalentes
                    # TODO: detectar complexos metalicos
                    # TODO: detectar CLASH

                    all_inter = calc_all_interactions(src_grps,
                                                      trgt_grps,
                                                      conf=BOUNDARY_CONF)

                    # Then it applies a filtering function.
                    filtered_inter = apply_interaction_criteria(all_inter, conf=self.interaction_conf)
                    inter_to_be_stored = all_inter
                else:
                    # It filters the interactions by using a cutoff at once.
                    filtered_inter = calc_all_interactions(src_grps, trgt_grps, conf=self.interaction_conf)
                    inter_to_be_stored = filtered_inter

                # TODO: Remover comentario
                # proj_inter_manager.insert_interactions(inter_to_be_stored, db_ligand_entity)

            # TODO: Ou uso STORE ou INSERT. Tenho que padronizar

            #
            # Count the number of compound types for each ligand
            #
            comp_type_count = count_group_types(grps_by_compounds[ligand], comp_type_id_map)
            # self.store_compound_statistics(ligand_entry.id,
            #                                comp_type_count)
            comp_type_count_by_entry[ligand_entry] = comp_type_count

            #
            # Count the number of interactions for each type
            #
            inter_type_count = count_interaction_types(filtered_inter, {ligand}, inter_type_id_map)
            # Store the count into the DB
            # self.store_interaction_statistics(ligand_entry.id, inter_type_count)
            inter_type_count_by_entry[ligand_entry] = inter_type_count

            # TODO: Atualizar outros scripts que verificam se um residuo é proteina ou nao.
            #       Agora temos uma função propria dentro de Residue.py
            interacting_residues = get_interacting_residues(filtered_inter, {ligand})
            inter_res_by_entry[ligand_entry] = interacting_residues

            # PAREI AQUI:
            # PROXIMO PASSO: contabilizar pra cada cluster o numero de ligantes que interagem com o residuo

            # TODO: Definir uma regĩao em volta do ligante pra ser o sitio ativo
            # TODO: Contar pra cada sitio o numero de cada tipo de grupo
            # TODO: Contar pra cada sitio o numero de interacoes

            # Select ligand and read it in a RDKit molecule object
            rdmol_lig = self.get_rdkit_mol(structure[0], ligand, ligand_entry.to_string(ENTRIES_SEPARATOR))

            # Generate fingerprint for the ligand
            fp = self.get_fingerprint(rdmol_lig)
            fingerprints.append(fp)

            # self.generate_ligand_figure(rdmol_lig, grps_by_compounds[ligand])

        # TODO: Remover entradas repetidas
        # TODO: remover Entradas que derem erro

        # Clusterize ligands by using the Botina clustering method.
        clusters = self.clusterize_ligands(fingerprints)

        comp_type_freq_by_cluster = defaultdict(int)
        inter_type_freq_by_cluster = defaultdict(int)
        inter_res_freq_by_cluster = defaultdict(InteractingResidues)

        for ligand_entry in self.entries:
            cluster_id = clusters[ligand_entry.to_string(ENTRIES_SEPARATOR)]

            # TODO: REMOVER
            cluster_id = 1

            # Get the ligand entry to update the cluster information
            if self.get_entries_from_db is False:
                db_ligand_entity = self.recover_ligand_entry(ligand_entry)
            else:
                db_ligand_entity = db_lig_entries_by_id[ligand_entry.id]

            db_ligand_entity.cluster = cluster_id

            comp_type_count = comp_type_count_by_entry[ligand_entry]
            # self.update_freq_by_cluster(comp_type_count, cluster_id, comp_type_freq_by_cluster)

            inter_type_count = inter_type_count_by_entry[ligand_entry]
            # self.update_freq_by_cluster(inter_type_count, cluster_id, inter_type_freq_by_cluster

            # TODO: Pra continuar fazendo isso vou precisar do alinhamento.
            # Vou fazer um alinhamento ficticio
            # self.update_res_freq_by_cluster(interacting_residues, inter_res_freq_by_cluster[cluster_id])

            # print("#######")
            # print(ligand_entry)
            # for k in interacting_residues.level1:
            #     print(k, k.__str__)
            # print()
            # for k in inter_res_freq_by_cluster[cluster_id].level1:
            #     print(k, inter_res_freq_by_cluster[cluster_id].level1[k])
            # print()
            # print()

        # Approve the cluster updates.
        self.db.approve_session()

        # Save the compound and interaction type frequency into the DB
        self.store_comp_type_freq(comp_type_freq_by_cluster)
        self.store_inter_type_freq(inter_type_freq_by_cluster)

        # TODO: alinhar proteína com o template
        # TODO: inserir alinhamento no BD

        # TODO: Calcular a frequencia dos residuos
        # TODO: Adicionar informações de frequencia no banco

    def update_res_freq_by_cluster(self, interacting_residues, inter_res_freq):
        inter_res_freq.update(interacting_residues)

    def recover_ligand_entry(self, ligand_entry):
        if isinstance(ligand_entry, DBEntry) is False:
            raise InvalidNapoliEntry("Entry '%s' is not a DBEntry object. So, nAPOLI cannot obtain "
                                     "an id information for this ligand entry and no database update will be "
                                     "possible." % entry_str)

        db_ligand_entity = (self.db.session
                            .query(LigandEntry)
                            .filter(LigandEntry.id == ligand_entry.id and
                                    LigandEntry.inter_proj_project_id == self.project_id and
                                    LigandEntry.pdb_id == ligand_entry.pdb_id and
                                    LigandEntry.chain_id == ligand_entry.chain_id and
                                    LigandEntry.lig_name == ligand_entry.lig_name and
                                    LigandEntry.lig_num == ligand_entry.lig_num and
                                    LigandEntry.lig_icode == ligand_entry.lig_icode)
                            .first())

        # If this entry does not exist in the database,
        # raise an error.
        if db_ligand_entity is None:
            message = ("Entry '%s' does not exist in the database." % ligand_entry.to_string(ENTRIES_SEPARATOR))
            raise InvalidNapoliEntry(message)

        return db_ligand_entity


class Fingerprint_PLI_Project(InteractionsProject):

    def __init__(self, entries, working_path, mfp_output=None, ifp_output=None, **kwargs):

        self.project_type = "PLI analysis"
        self.mfp_output = mfp_output
        self.ifp_output = ifp_output

        super().__init__(entries=entries, working_path=working_path, **kwargs)

    def _process_entries(self, entries):
        for ligand_entry in entries:
            self.current_entry = ligand_entry

            # # Check if the entry is in the correct format.
            # # It also accepts entries whose pdb_id is defined by the filename.
            self.validate_entry_format(ligand_entry)

            # # TODO: allow the person to pass a pdb_file into entries.
            pdb_file = self.get_pdb_file(ligand_entry.pdb_id)

            # # # TODO: resolver o problema dos hidrogenios.
            # # #       Vou adicionar hidrogenio a pH 7?
            # # #       Usuario vai poder definir pH?
            structure = PDB_PARSER.get_structure(ligand_entry.pdb_id, pdb_file)
            add_hydrogen = self.decide_hydrogen_addition(PDB_PARSER.get_header())

            if isinstance(ligand_entry, MolEntry):
                structure = ligand_entry.get_biopython_structure(structure, PDB_PARSER)

            ligand = self.get_target_entity(structure, ligand_entry)
            ligand.set_as_target(is_target=True)

            grps_by_compounds = self.perceive_chemical_groups(structure[0], ligand, add_hydrogen)
            src_grps = [grps_by_compounds[x] for x in grps_by_compounds if x.get_id()[0] != " "]
            trgt_grps = [grps_by_compounds[x] for x in grps_by_compounds]

            # # # Calculate interactions
            inter_filter = InteractionFilter.new_pli_filter(inter_conf=self.interaction_conf, ignore_self_inter=False)
            ic = InteractionCalculator(inter_conf=self.interaction_conf, inter_filter=inter_filter,
                                       add_non_cov=self.add_non_cov, add_atom_atom=self.add_atom_atom,
                                       add_proximal=self.add_proximal)
            interactions = ic.calc_interactions(src_grps, trgt_grps)

            result = {"id": (ligand_entry.full_id)}
            if self.calc_mfp:
                if isinstance(ligand_entry, MolEntry):
                    rdmol_lig = MolFromSmiles(str(ligand_entry.mol_obj).split("\t")[0])
                    rdmol_lig.SetProp("_Name", ligand_entry. mol_id)
                    result["mfp"] = generate_fp_for_mols([rdmol_lig], "morgan_fp")[0]["fp"]
                else:
                    # Read from PDB.
                    pass

            if self.calc_ifp:
                neighborhood = [ag for c in grps_by_compounds.values() for ag in c.atm_grps]
                shells = ShellGenerator(self.ifp_num_levels, self.ifp_radius_step)
                sm = shells.create_shells(neighborhood)
                result["ifp"] = sm.to_fingerprint(fold_to_size=self.ifp_length)

            if isinstance(ligand_entry, MolEntry):
                result["smiles"] = str(ligand_entry.mol_obj).split("\t")[0]
            else:
                # Get Smiles from PDB
                pass

            self.result.append(result)

    def __call__(self):

        if not self.calc_mfp and not self.calc_ifp:
            logger.warning("Both molecular and interaction fingerprints were turned off. So, there is nothing to be done...")
            return

        self.prepare_project_path()
        self.init_logging_file("%s/%s" % (self.working_path, "logging.log"))

        self.set_pharm_objects()

        free_filename_format = self.has_local_files
        self.entry_validator = PLIEntryValidator(free_filename_format)

        fingerprints = []
        pli_fingerprints = []

        comp_type_count_by_entry = {}
        inter_type_count_by_entry = {}
        inter_res_by_entry = {}

        if self.preload_mol_files:
            self.add_mol_obj_to_entries()

        manager = mp.Manager()
        self.result = manager.list()

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
                OUT.write("ligand_id, smiles, on_bits\n")
                for r in self.result:
                    if "mfp" in r:
                        fp_str = "\t".join([str(x) for x in r["mfp"].GetOnBits()])
                        OUT.write("%s:%s,%s,%s\n" % (r["id"][0], r["id"][1], r["smiles"], fp_str))

        if self.calc_ifp:
            self.ifp_output = self.ifp_output or "%s/results/ifp.csv" % self.working_path
            with open(self.ifp_output, "w") as OUT:
                OUT.write("ligand_id, smiles, on_bits\n")
                for r in self.result:
                    if "ifp" in r:
                        fp_str = "\t".join([str(x) for x in r["ifp"].get_on_bits()])
                        OUT.write("%s:%s,%s,%s\n" % (r["id"][0], r["id"][1], r["smiles"], fp_str))

        end = time.time()
        print(end - start)


class Local_PLI_Project(InteractionsProject):

    def __init__(self, entries, working_path, has_local_files=False, **kwargs):

        self.project_type = "PLI analysis"

        super().__init__(entries=entries, working_path=working_path, has_local_files=has_local_files, **kwargs)

    def __call__(self):

        # TODO: verificar o numero de entradas

        # TODO: Definir as mensagens de descrição para o usuário
        # TODO: colocar wrapers em cada função

        # TODO: Remove duplicated entries.

        # TODO: add a new parameter: hydrogen addition mode to define how hydrogens will be added OR:
        #       add a new parameter: force hydrogen addition.

        # TODO: logging is not working. The logs are not being directed to the logging.log file.

        self.prepare_project_path()
        self.init_logging_file("%s/%s" % (self.working_path, "logging.log"))

        self.set_pharm_objects()

        free_filename_format = self.has_local_files
        self.entry_validator = PLIEntryValidator(free_filename_format)

        fingerprints = []
        pli_fingerprints = []

        comp_type_count_by_entry = {}
        inter_type_count_by_entry = {}
        inter_res_by_entry = {}

        if self.preload_mol_files:
            self.add_mol_obj_to_entries()

        import time

        # Loop over each entry.
        for ligand_entry in self.entries:
            start = time.time()

            logger.info("Processing entry: %s." % ligand_entry)
            print("Processing entry: %s." % ligand_entry.mol_id)
            self.current_entry = ligand_entry

            # Check if the entry is in the correct format.
            # It also accepts entries whose pdb_id is defined by the filename.
            self.validate_entry_format(ligand_entry)

            # TODO: allow the person to pass a pdb_file into entries.
            pdb_file = self.get_pdb_file(ligand_entry.pdb_id)

            # # TODO: resolver o problema dos hidrogenios.
            # #       Vou adicionar hidrogenio a pH 7?
            # #       Usuario vai poder definir pH?
            structure = PDB_PARSER.get_structure(ligand_entry.pdb_id, pdb_file)
            add_hydrogen = self.decide_hydrogen_addition(PDB_PARSER.get_header())

            if isinstance(ligand_entry, MolEntry):
                structure = ligand_entry.get_biopython_structure(structure, PDB_PARSER)

            ligand = self.get_target_entity(structure, ligand_entry)
            ligand.set_as_target(is_target=True)

            grps_by_compounds = self.perceive_chemical_groups(structure[0], ligand, add_hydrogen)
            src_grps = [grps_by_compounds[x] for x in grps_by_compounds if x.get_id()[0] != " "]
            trgt_grps = [grps_by_compounds[x] for x in grps_by_compounds]

            # # Calculate interactions
            inter_filter = InteractionFilter.new_pli_filter(inter_conf=self.interaction_conf, ignore_self_inter=False)
            ic = InteractionCalculator(inter_conf=self.interaction_conf, inter_filter=inter_filter,
                                       add_non_cov=self.add_non_cov, add_atom_atom=self.add_atom_atom,
                                       add_proximal=self.add_proximal)
            interactions = ic.calc_interactions(src_grps, trgt_grps)

            neighborhood = [ag for c in grps_by_compounds.values() for ag in c.atm_grps]
            shells = ShellGenerator(10, 1)
            sm = shells.create_shells(neighborhood)
            fp = sm.to_fingerprint(fold_to_size=IFP_LENGTH)

            pli_fingerprints.append((ligand_entry, fp))

            end = time.time()
            print("> Processing time:", (end - start))
            print()
            continue

            #
            # Count the number of compound types for each ligand
            #
            # comp_type_count = count_group_types(grps_by_compounds[ligand])
            # comp_type_count_by_entry[ligand_entry] = comp_type_count

            #
            # Count the number of interactions for each type
            #
            # inter_type_count = count_interaction_types(filtered_inter,
            #                                            {ligand})
            # inter_type_count_by_entry[ligand_entry] = inter_type_count

            # TODO: Atualizar outros scripts que verificam se um residuo é proteina ou nao.
            #       Agora temos uma função propria dentro de Residue.py
            # interacting_residues = get_interacting_residues(filtered_inter, {ligand})
            # inter_res_by_entry[ligand_entry] = interacting_residues

            # PAREI AQUI:
            # PROXIMO PASSO: contabilizar pra cada cluster o numero de ligantes que interagem com o residuo

            # TODO: Definir uma regĩao em volta do ligante pra ser o sitio ativo
            # TODO: Contar pra cada sitio o numero de cada tipo de grupo
            # TODO: Contar pra cada sitio o numero de interacoes

            # Select ligand and read it in a RDKit molecule object.

            # TODO: I cannot use RDKit directly in the PDB file or it will crash the molecule.
            #       First I need to convert it to MOL.
            # rdmol_lig = self.get_rdkit_mol(structure[0], ligand, ligand_entry.to_string(ENTRIES_SEPARATOR))

            # Generate fingerprint for the ligand
            # fp = self.get_fingerprint(rdmol_lig)
            # fingerprints.append(fp)

            # Generate fingerprint for the ligand.

            if isinstance(ligand_entry, MolEntry):
                rdmol_lig = MolFromSmiles(str(ligand_entry.mol_obj).split("\t")[0])
                rdmol_lig.SetProp("_Name", ligand_entry. mol_id)
                fps = generate_fp_for_mols([rdmol_lig], "morgan_fp")
                fingerprints.append((ligand_entry, fps[0]["fp"]))



            # self.generate_ligand_figure(rdmol_lig, grps_by_compounds[ligand])

        with open("ecfp4_fingerprints.csv", "w") as OUT:
            OUT.write("id,smarts,fp\n")
            for (entry, fp) in fingerprints:
                fp_str = "\t".join([str(x) for x in fp.GetOnBits()])
                OUT.write("%s,%s,%s\n" % (entry.mol_id, str(entry.mol_obj).split("\t")[0], fp_str))
        exit()

        with open("ifp_fingerprints.csv", "w") as OUT:
            OUT.write("id,smarts,fp\n")
            for (entry, fp) in pli_fingerprints:
                fp_str = "\t".join([str(x) for x in fp.get_on_bits()])
                OUT.write("%s,%s,%s\n" % (entry.mol_id, str(entry.mol_obj).split("\t")[0], fp_str))

        print("DONE!!!!")
        exit()

        # TODO: Remover entradas repetidas
        # TODO: remover Entradas que derem erro

        # Clusterize ligands by using the Botina clustering method.
        # clusters = self.clusterize_ligands(fingerprints)

        # comp_type_freq_by_cluster = defaultdict(int)
        # inter_type_freq_by_cluster = defaultdict(int)
        # inter_res_freq_by_cluster = defaultdict(InteractingResidues)

        # for ligand_entry in self.entries:
        #     cluster_id = clusters[ligand_entry.to_string(ENTRIES_SEPARATOR)]

        #     # TODO: REMOVER
        #     cluster_id = 1

        #     # Get the ligand entry to update the cluster information
        #     if self.get_entries_from_db is False:
        #         db_ligand_entity = self.recover_ligand_entry(ligand_entry)
        #     else:
        #         db_ligand_entity = db_lig_entries_by_id[ligand_entry.id]

        #     db_ligand_entity.cluster = cluster_id

        #     comp_type_count = comp_type_count_by_entry[ligand_entry]
        #     # self.update_freq_by_cluster(comp_type_count, cluster_id,
        #                                 # comp_type_freq_by_cluster)

        #     inter_type_count = inter_type_count_by_entry[ligand_entry]
            # self.update_freq_by_cluster(inter_type_count, cluster_id,
                                        # inter_type_freq_by_cluster

            # TODO: Pra continuar fazendo isso vou precisar do alinhamento.
            # Vou fazer um alinhamento ficticio
            # self.update_res_freq_by_cluster(interacting_residues,
            #                                 inter_res_freq_by_cluster[cluster_id])

            # print("#######")
            # print(ligand_entry)
            # for k in interacting_residues.level1:
            #     print(k, k.__str__)
            # print()
            # for k in inter_res_freq_by_cluster[cluster_id].level1:
            #     print(k, inter_res_freq_by_cluster[cluster_id].level1[k])
            # print()
            # print()

        # TODO: alinhar proteína com o template
        # TODO: inserir alinhamento no BD

        # TODO: Calcular a frequencia dos residuos
        # TODO: Adicionar informações de frequencia no banco

    def update_res_freq_by_cluster(self, interacting_residues, inter_res_freq):
        inter_res_freq.update(interacting_residues)


class NAPOLI_PLI:

    def __init__(self,
                 entries=None,
                 pdb_path=None,
                 working_path=None,
                 overwrite_path=False,
                 job_code=None,
                 db_conf_file=None,
                 populate_rcsb_tables=False,
                 pdb_template=None,
                 atom_prop_file=ATOM_PROP_FILE,
                 interaction_conf=INTERACTION_CONF,
                 # force_calc_interactions=False,
                 save_all_interactions=True,
                 ph=None,
                 mfp_func="pharm2d_fp",
                 similarity_func="BulkTanimotoSimilarity",
                 run_from_step=None,
                 run_until_step=None):

        self.entries = entries

        self.pdb_path = pdb_path
        self.working_path = working_path
        self.overwrite_path = overwrite_path
        self.job_code = job_code
        self.db_conf_file = db_conf_file

        self.populate_rcsb_tables = populate_rcsb_tables

        self.pdb_template = pdb_template
        self.atom_prop_file = atom_prop_file
        self.interaction_conf = interaction_conf
        self.save_all_interactions = save_all_interactions
        self.ph = ph
        self.add_hydrog = True
        self.mfp_func = mfp_func
        self.similarity_func = similarity_func
        self.run_from_step = run_from_step
        self.run_until_step = run_until_step

    def run(self):
        # TODO: Create functions for reduce the amount of code in the run()
        try:
            if self.job_code and self.populate_rcsb_tables:
                logger.info("You informed a job code and set RCSB's population mode to true. "
                            "However, a job code has a higher prority over the latter. "
                            "So, the RCSB tables will not be populated.")
                self.populate_rcsb_tables = False

            if self.job_code and not self.db_conf_file:
                raise IllegalArgumentError("You informed a job code, but "
                                           "none database configuration file "
                                           "was informed. So, it would not "
                                           "be possible to store interactions "
                                           "and related information into the "
                                           "database.")
            elif self.populate_rcsb_tables and not self.db_conf_file:
                raise IllegalArgumentError("You activate the RCSB's "
                                           "population mode, but none "
                                           "database configuration file "
                                           "was informed. So, it would not "
                                           "be possible to store interactions "
                                           "and related information into the "
                                           "database.")
            elif not self.job_code and not self.working_path:
                raise IllegalArgumentError("Neither a job code or a working "
                                           "path was defined."
                                           "Without them, it is not possible "
                                           "to decide which output directory "
                                           "to use.")

            if self.populate_rcsb_tables:
                logger.info("RCSB's population mode active. "
                            "Interactions and related information will "
                            "be stored into the defined database without "
                            "relating them to a job code.")

            ##########################################################
            step = "Prepare project directory"
            if self.working_path:
                working_path = self.working_path
            else:
                working_path = "%s/projects/%s" % (NAPOLI_PATH, self.job_code)

            working_pdb_path = "%s/pdbs" % working_path
            create_directory(working_path, self.overwrite_path)
            create_directory(working_pdb_path)

            logger.info("Results and temporary files will be saved at '%s'." % working_path)

            # Set the LOG file at the working path
            # logfile = "%s/napoli.log" % working_path
            # filehandler = logging.FileHandler(logfile, 'a')
            # formatter = logging.Formatter('%(asctime)s - %(name)s - '
            #                               '%(levelname)s - %(filename)s - '
            #                               '%(funcName)s - %(lineno)d => '
            #                               '%(message)s')
            # filehandler.setFormatter(formatter)
            # # Remove the existing file handlers
            # for hdlr in logger.handlers[:]:
            #     if isinstance(hdlr, logger.FileHander):
            #         logger.removeHandler(hdlr)
            # logger.addHandler(filehandler)      # set the new handler
            # # Set the log level to INFO, DEBUG as the default is ERROR
            # logger.setLevel("INFO")

            # logger.info("Logs will be saved in '%s'." % logfile)

            # TODO: avisar se eh csv ou salvar no BD.
            # OUTPUT MODE:
            if self.job_code:
                logger.info("A job code ('%s') was informed. nAPOLI will try to save all results "
                            "in the database using such id." % self.job_code)

            # Define which PDB path to use.
            if self.pdb_path is None:
                # If none PDB path was defined and none DB conf was defined
                # it is better to create a new directory to avoid
                # overwriting Public directories
                if self.db_conf_file is None:
                    pdb_path = working_pdb_path
                else:
                    pdb_path = PDB_PATH
            else:
                pdb_path = self.pdb_path

            logger.info("PDB files will be obtained from and/or downloaded "
                        "to '%s'." % pdb_path)

            ##########################################################
            if self.db_conf_file:
                step = "Preparing database"
                logger.info("A database configuration file was defined. "
                            "Starting a new database connection...")

                config = Config(self.db_conf_file)
                dbConf = config.get_section_map("database")
                db = DBLoader(**dbConf)

                rcsb_inter_manager = RCSBInteractionManager(db)
                proj_inter_manager = ProjectInteractionManager(db)

                # TODO: create all mappers at once
                db.new_mapper(CompTypeCount, "comp_type_count")
                db.new_mapper(InterTypeCount, "inter_type_count")
                db.new_mapper(Project, "project")
                db.new_mapper(LigandEntry, "ligand_entry")
                db.new_mapper(Status, "status")

                if self.job_code:
                    projectRow = (db.session
                                  .query(Project)
                                  .filter(Project.job_code ==
                                          self.job_code).one())
                    projectId = projectRow.id

                interTypeRows = db.session.query(InterType).all()
                interIdByType = {r.type: r.id for r in interTypeRows}
                interFilters = default_interaction_filters(interIdByType,
                                                           self.interaction_conf)

                compTypeRows = db.session.query(CompoundType).all()
                compIdByType = {r.type: r.id for r in compTypeRows}

                status_rows = db.session.query(Status).all()
                status_by_id = {r.id: r.title for r in status_rows}

                if self.entries is None:
                    if self.job_code is None:
                        raise IllegalArgumentError("No ligand entry list was "
                                                   "defined. "
                                                   "The program could try to "
                                                   "recover a list from the "
                                                   "database, but no job code "
                                                   "was defined either.")
                    else:
                        logger.info("No ligand entry was defined. "
                                    "It will try to recover a ligand entry "
                                    "list from the database.")
                        db_ligand_entries = (LigandEntryManager(db)
                                             .get_entries(projectId))
                        self.entries = format_db_ligand_entries(db_ligand_entries)

            logger.info("Number of ligand entries to be processed: %d." %
                        len(self.entries))

            if self.add_hydrog:
                if self.ph:
                    logger.info("Hydrogen atoms will be added to the "
                                "structures according to the pH %s." % self.ph)
                else:
                    logger.info("A pH value was not defined. "
                                "Hydrogen atoms will be added to the "
                                "structures without considering a pH "
                                "value.")
            else:
                logger.info("Option ADD_HYDROG disabled. nAPOLI will not add "
                            "hydrogen atoms to the structures.")

            ##########################################################
            # Set some variables
            validator = PLIEntryValidator()
            pdb_parser = PDBParser(PERMISSIVE=True,
                                   QUIET=True,
                                   FIX_ATOM_NAME_CONFLICT=False,
                                   FIX_OBABEL_FLAGS=False)

            feat_factory = (ChemicalFeatures.BuildFeatureFactory(self.atom_prop_file))

            sigFactory = SigFactory(feat_factory, minPointCount=2,
                                    maxPointCount=3,
                                    trianglePruneBins=False)
            sigFactory.SetBins([(0, 2), (2, 5), (5, 8)])
            sigFactory.Init()

            feat_extractor = FeatureExtractor(feat_factory)

            boundary_conf = InteractionConf({"boundary_cutoff": 7})

            fingerprints = []

            # The calculus of interactions depend on the pharmacophore
            # definition (ATOM_PROP_FILE) and the pH (PH).
            # Thus, if the user has kept such values unchanged and has
            # not uploaded any PDB file, nAPOLI can try to select the
            # pre-computed interactions from a database.
            is_inter_params_default = (self.pdb_path is None and
                                       self.atom_prop_file == ATOM_PROP_FILE and
                                       self.ph is None and
                                       self.db_conf_file is not None)

            if is_inter_params_default:
                logger.info("Default configuration was kept unchanged. "
                            "nAPOLI will try to select pre-computed "
                            "interactions from the defined database")

            ##########################################################

            # Loop over each entry.
            for ligand_entry in self.entries:
                try:
                    entry_str = ligand_entry.to_string(ENTRIES_SEPARATOR)
                    myBioLigand = ("H_%s" % ligand_entry.lig_name,
                                   ligand_entry.lig_num,
                                   ligand_entry.lig_icode)

                    # TODO: Verificar se ligante existe no modelo (BioPDB)

                    db_ligand_entity = None
                    if self.db_conf_file and self.job_code:
                        step = "Check ligand entry existance"
                        if isinstance(ligand_entry, DBEntry) is False:
                            message = ("Entry '%s' is not a DBEntry "
                                       "object. So, nAPOLI cannot obtain an "
                                       "id information for this ligand entry "
                                       "and no database update will be "
                                       "possible." % entry_str)
                            raise InvalidNapoliEntry(message)
                        elif ligand_entry.id is None:
                            message = ("An invalid id for the entry '%s'"
                                       "was defined." % entry_str)
                            raise InvalidNapoliEntry(message)
                        else:
                            db_ligand_entity = (db.session
                                                .query(LigandEntry)
                                                .filter(LigandEntry.id == ligand_entry.id)
                                                .first())
                            if db_ligand_entity is None:
                                message = ("Entry '%s' with id equal "
                                           "to '%d' does not exist in the "
                                           "database."
                                           % (entry_str, ligand_entry.id))
                                raise InvalidNapoliEntry(message)
                    elif self.populate_rcsb_tables:
                        step = "Check ligand existance in RCSB tables"
                        db_ligand_entity = (db.session
                                            .query(Ligand)
                                            .filter(Ligand.pdb_id == ligand_entry.pdb_id and
                                                    Ligand.chain_id == ligand_entry.chain_id and
                                                    Ligand.lig_name == ligand_entry.lig_name and
                                                    Ligand.lig_num == ligand_entry.lig_num and
                                                    Ligand.lig_icode == ligand_entry.lig_icode)
                                            .first())

                        # If this entry does not exist in the database,
                        # raise an error.
                        if db_ligand_entity is None:
                            message = ("Entry '%s' does not exist in the "
                                       "table 'ligand'." % entry_str)
                            raise InvalidNapoliEntry(message)
                        # If there are already interactions to this entry,
                        # raise an error.
                        elif status_by_id[db_ligand_entity.status_id] == "AVAILABLE":
                            raise DuplicateEntry("Interactions to the entry '%s' already exists in "
                                                 "the database." % entry_str)

                    ##########################################################
                    step = "Validate nAPOLI entry"
                    if not validator.is_valid(entry_str):
                        raise InvalidNapoliEntry("Entry '%s' does not match the nAPOLI entry format." % entry_str)

                    ##########################################################
                    # TODO: option to change name of default PDB name.
                    #      BioPython cria arquivo com .env
                    step = "Check PDB file existance"
                    # User has defined a specific directory.
                    if self.pdb_path:
                        pdb_file = "%s/%s.pdb" % (pdb_path, ligand_entry.pdb_id)
                        # If the file does not exist or is invalid.
                        if is_file_valid(pdb_file) is False:
                            raise FileNotFoundError("The PDB file '%s' was not found." % pdb_file)
                    # If none DB conf. was defined try to download the PDB.
                    # Here, pdb_path is equal to the working_pdb_path
                    elif self.db_conf_file is None:
                        pdb_file = "%s/pdb%s.ent" % (working_pdb_path, ligand_entry.pdb_id.lower())
                        download_pdb(ligand_entry.pdb_id, working_pdb_path)
                    else:
                        pdb_file = "%s/pdb%s.ent" % (pdb_path, ligand_entry.pdb_id.lower())

                        # If the file does not exist or is invalid, it is
                        # better to download a new PDB file at the working path.
                        if not is_file_valid(pdb_file):
                            pdb_file = "%s/pdb%s.ent" % (working_pdb_path, ligand_entry.pdb_id.lower())
                            download_pdb(ligand_entry.pdb_id, working_pdb_path)
                        else:
                            # TODO: check if PDB is updated on the DB
                            pass

                    #####################################
                    step = "Prepare protein structure"
                    # TODO: Distinguish between files that need to remove hydrogens (NMR, for example).
                    # TODO: Add parameter to force remove hydrogens (user marks it)
                    if self.add_hydrog:
                        preppdb_file = "%s/%s.H.pdb" % (working_pdb_path, ligand_entry.pdb_id)

                        if not exists(preppdb_file):
                            if self.ph:
                                obOpt = {"p": 7, "error-level": 5}
                            else:
                                obOpt = {"h": None, "error-level": 5}

                            convert_molecule(pdb_file, preppdb_file, opt=obOpt)

                        pdb_file = preppdb_file

                    ##########################################################
                    step = "Parse PDB file"
                    structure = pdb_parser.get_structure(ligand_entry.pdb_id, pdb_file)
                    ligand = structure[0][ligand_entry.chain_id][myBioLigand]
                    ligand.set_as_target()

                    ##########################################################
                    step = "Generate structural fingerprint"
                    lig_sel = ResidueSelector({ligand})
                    lig_block = entity_to_string(structure, lig_sel, write_conects=False)
                    rdLig = MolFromPDBBlock(lig_block)
                    rdLig.SetProp("_Name", entry_str)
                    fpOpt = {"sigFactory": sigFactory}
                    fp = generate_fp_for_mols([rdLig], self.mfp_func,
                                              fpOpt=fpOpt, critical=True)[0]
                    fingerprints.append(fp)

                    ##########################################################
                    step = "Perceive chemical groups"
                    nb_compounds = get_contacts_for_entity(structure[0],
                                                           ligand,
                                                           level='R')

                    compounds = set([x[1] for x in nb_compounds])
                    grps_by_compounds = {}
                    for comp in compounds:
                        # TODO: verificar se estou usando o ICODE nos residuos
                        groups = find_compound_groups(comp, feat_extractor)
                        grps_by_compounds[comp] = groups

                    trgt_grps = [grps_by_compounds[x] for x in grps_by_compounds if x.is_water() or x.is_hetatm()]
                    nb_grps = [grps_by_compounds[x] for x in grps_by_compounds if x != ligand]

                    # TODO: Preparar estrutura somente se não for selecionar
                    # da base de dados

                    ##########################################################
                    step = "Calculate interactions"

                    calculate_interactions = True
                    if (is_inter_params_default and
                            not self.populate_rcsb_tables):
                        logger.info("Trying to select pre-computed interactions for "
                                    "the entry '%s'." % entry_str)

                        db_ligand_entity = (db.session
                                            .query(Ligand)
                                            .filter(Ligand.pdb_id == ligand_entry.pdb_id and
                                                    Ligand.chain_id == ligand_entry.chain_id and
                                                    Ligand.lig_name == ligand_entry.lig_name and
                                                    Ligand.lig_num == ligand_entry.lig_num and
                                                    Ligand.lig_icode == ligand_entry.lig_icode)
                                            .first())

                        if db_ligand_entity:
                            if status_by_id[db_ligand_entity.status_id] == "AVAILABLE":
                                join_filter = get_ligand_tbl_join_filter(ligand_entry, Ligand)
                                db_interactions = (rcsb_inter_manager
                                                   .select_interactions(join_filter,
                                                                        interFilters))
                                filtered_inter = format_db_interactions(structure, db_interactions)

                                logger.info("%d pre-computed interaction(s) found in the "
                                            "database for the entry '%s'."
                                            % (len(filtered_inter), entry_str))

                                calculate_interactions = False
                            else:
                                logger.info("The entry '%s' exists in the database, but "
                                            "there is no pre-computed interaction available. "
                                            "So, nAPOLI will calculate the interactions to this "
                                            "ligand." % entry_str)
                        else:
                            logger.info("The entry '%s' does not exist in the "
                                        "database." % entry_str)

                    if calculate_interactions:
                        if self.save_all_interactions:
                            # First it calculates all interactions using a
                            # boundary limit. When using a database, it is
                            # useful to save all potential interactions.
                            # So, it is possible to filter interactions
                            # faster than recalculate them for each
                            # modification in the interaction criteria.
                            all_inter = calc_all_interactions(trgt_grps,
                                                              nb_grps,
                                                              conf=boundary_conf)

                            # Then it applies a filtering function.
                            filtered_inter = apply_interaction_criteria(all_inter,
                                                                        conf=self.interaction_conf)

                            db_interactions = all_inter
                        else:
                            # It filters the interactions by using a cutoff at once.
                            filtered_inter = calc_all_interactions(trgt_grps,
                                                                   nb_grps,
                                                                   conf=self.interaction_conf)
                            db_interactions = filtered_inter

                        ##########################################################
                        if self.db_conf_file and self.job_code:
                            #TODO: Remover comentario
                            #TODO: Adicionar ICODE nas chaves de entrada que o usuario submete
                            # proj_inter_manager.insert_interactions(db_interactions, db_ligand_entity)
                            pass
                        elif self.populate_rcsb_tables:
                            # rcsb_inter_manager.insert_interactions(db_interactions, db_ligand_entity)
                            pass
                        else:
                            #TODO: Criar arquivo de saida formato .tsv
                            pass

                    if not self.populate_rcsb_tables:
                        ##########################################################
                        step = "Calculate atom type statistics"
                        ligand_grps = grps_by_compounds[ligand]
                        grp_types = count_group_types(ligand_grps, compIdByType)
                        if self.db_conf_file:
                            for typeId in grp_types:
                                db.session.add(CompTypeCount(ligand_entry.id, projectId,
                                                             typeId, grp_types[typeId]))
                            # TODO: Remover comentario
                            db.approve_session()

                        ##########################################################
                        step = "Calculate interaction type statistics"
                        summary_trgts = {ligand}
                        summary_trgts = None
                        interaction_types = count_interaction_types(filtered_inter,
                                                                    summary_trgts,
                                                                    interIdByType)
                        if self.db_conf_file:
                            for typeId in interaction_types:
                                db.session.add(InterTypeCount(ligand_entry.id, projectId,
                                                              typeId, interaction_types[typeId]))

                            #TODO: Remover comentario
                            db.approve_session()

                        print("Last step: '%s'" % step)

                except Exception as e:
                    logger.exception(e)

                    # TODO: Inform that the Entry failed

            ##########################################################
            # TODO: after clusterize molecules to align the ligands in a same way.

            # step = "Generate 2D ligand structure"
            # # pdbExtractor = Extractor(structure[0][ligand_entry.chain])
            # # ligandpdb_file = "%s/%s.pdb" % (working_pdb_path, entry)
            # # pdbExtractor.extract_residues([ligandBio], ligandpdb_file)
            # lig_sel = ResidueSelector({ligand})
            # lig_block = entity_to_string(structure, lig_sel,
            #                              write_conects=False)
            # rdLig = MolFromPDBBlock(lig_block)
            # Compute2DCoords(rdLig)

        except Exception as e:
            logger.exception(e)
