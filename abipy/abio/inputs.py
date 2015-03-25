"""
This module defines objects that faciliate the creation of the 
ABINIT input files. The syntax is similar to the one used 
in ABINIT with small differences. 
"""
from __future__ import print_function, division, unicode_literals

import os
import collections
import warnings
import itertools
import copy
import six
import abc
import json
import numpy as np

from collections import OrderedDict, MutableMapping
from monty.collections import dict2namedtuple
from monty.string import is_string, list_strings
from pymatgen.core.units import Energy
from pymatgen.serializers.json_coders import PMGSONable, pmg_serialize
from pymatgen.io.abinitio.pseudos import PseudoTable, Pseudo
from pymatgen.io.abinitio.tasks import AbinitTask, ParalHintsParser
from pymatgen.io.abinitio.netcdf import NetcdfReader
from pymatgen.io.abinitio.abiinspect import yaml_read_irred_perts
from abipy.core.structure import Structure
from abipy.core.mixins import Has_Structure
from abipy.htc.variable import InputVariable
from abipy.abio.abivars import is_abivar, is_anaddb_var
from abipy.abio.abivars_db import get_abinit_variables

import logging
logger = logging.getLogger(__file__)


# List of Abinit variables used to specify the structure.
# This variables should not be passed to set_vars since 
# they will be generated with structure.to_abivars()
_GEOVARS = set([
    "acell",
    "rprim",
    "rprimd"
    "angdeg",
    "xred"
    "xcart"
    "xangst",
    "znucl",
    "typat",
    "ntypat",
    "natom",
])


class AbinitInputError(Exception):
    """Base error class for exceptions raised by `AbiInput`"""


class AbinitInput(six.with_metaclass(abc.ABCMeta, MutableMapping, PMGSONable, Has_Structure, object)):
    """
    This object stores the ABINIT variables for a single dataset.
    """
    Error = AbinitInputError

    def __init__(self, structure, pseudos, pseudo_dir=None, comment=None, decorators=None, vars=None):
        """
        Args:
            structure: Parameters defining the crystalline structure. Accepts :class:`Structure` object 
            file with structure (CIF, netcdf file, ...) or dictionary with ABINIT geo variables.
            pseudos: Pseudopotentials to be used for the calculation. Accepts: string or list of strings with the name 
                of the pseudopotential files, list of :class:`Pseudo` objects or :class:`PseudoTable` object.
            pseudo_dir: Name of the directory where the pseudopotential files are located.
            ndtset: Number of datasets.
            comment: Optional string with a comment that will be placed at the beginning of the file.
            decorators: List of `AbinitInputDecorator` objects.
            vars: Dictionary with the initial set of variables. Default: Empty
        """
        # Internal dict with variables. we use an ordered dict so that 
        # variables will be likely grouped by `topics` when we fill the input.
        vars = {} if vars is None else vars
        for key in vars:
            self._check_varname(key)

        self._vars = OrderedDict(**vars)

        self.set_structure(structure)

        if pseudo_dir is not None:
            pseudo_dir = os.path.abspath(pseudo_dir)
            if not os.path.exists(pseudo_dir): raise self.Error("Directory  %s does not exist")
            pseudos = [os.path.join(pseudo_dir, p) for p in list_strings(pseudos)]

        try:
            self._pseudos = PseudoTable.as_table(pseudos).get_pseudos_for_structure(self.structure)
        except ValueError as exc:
            raise self.Error(str(exc))

        if comment is not None: self.set_comment(comment)

        self._decorators = [] if not decorators else decorators

    @pmg_serialize
    def as_dict(self):
        vars = OrderedDict()
        for key, value in self.items():
            if isinstance(value, np.ndarray): value = value.tolist()
            vars[key] = value

        return dict(structure=self.structure.as_dict(),
                    pseudos=[p.as_dict() for p in self.pseudos], 
                    comment=self.comment,
                    decorators=[dec.as_dict() for dec in self.decorators],
                    vars=vars)

    @classmethod
    def from_dict(cls, d):
        pseudos = [Pseudo.from_file(p['filepath']) for p in d['pseudos']]
        return cls(d["structure"], pseudos, decorators=d["decorators"], comment=d["comment"], vars=d["vars"])

    # ABC protocol: __delitem__, __getitem__, __iter__, __len__, __setitem__
    def __delitem__(self, key):
        return self._vars.__delitem__(key)
        
    def __getitem__(self, key):
        return self._vars.__getitem__(key)

    def __iter__(self):
        return self._vars.__iter__()

    def __len__(self):
        return len(self._vars)

    def __setitem__(self, key, value):
        self._check_varname(key)
        return self._vars.__setitem__(key, value)

    def _check_varname(self, key):
        if not is_abivar(key):
            raise self.Error("%s is not a valid ABINIT variable.\n"
                             "If you are sure the name is correct, please contact the abipy developers\n" 
                             "or modify the JSON file abipy/abio/abinit_vars.json" % key)
                                                                                                                      
        if key in _GEOVARS:
            raise self.Error("You cannot set the value of a variable associated to the structure. Use set_structure")

    def __repr__(self):
        return "<%s at %s>" % (self.__class__.__name__, id(self))

    def __str__(self):
        return self.to_string()

    #def __eq__(self, other)
    #def __ne__(self, other)
    #    return not self.__eq__(other)

    #@abc.property
    #def runlevel(self):
    #    """String defining the Runlevel. See _runl2optdriver."""
    # Mapping runlevel --> optdriver variable
    #_runl2optdriver = {
    #    "scf": 0,
    #    "nscf": 0,
    #    "relax": 0,
    #    "dfpt": 1,
    #    "screening": 3,
    #    "sigma": 4,
    #    "bse": 99,
    #}
    #    # Find the value of optdriver (firt in self, then in globals finally use default value.
    #    optdriver = self.get("optdriver")
    #    if optdriver is None: optdriver = self.dt0.get("optdriver")
    #    if optdriver is None: optdriver = 0

    #    # At this point we have to understand the type of calculation.

    def deepcopy(self):
        """Deep copy of the input."""
        return copy.deepcopy(self)

    @property
    def decorators(self):
        return self._decorators

    def register_decorator(self, decorator):
        """Register a :class:`AbinitInputDecorator`."""
        self._decorators.append(decorator.as_dict())

    def set_mnemonics(self, boolean):
        """True if mnemonics should be printed"""
        self._mnemonics = bool(boolean)

    @property
    def mnemonics(self):
        """Return True if mnemonics should be printed"""
        try:
            return self._mnemonics
        except AttributeError:
            return False

    def to_string(self, sortmode=None, post=None, with_structure=True, with_pseudos=True):
        """
        String representation.

        Args:
            sortmode: "a" for alphabetical order, None if no sorting is wanted
            post: String that will be appended to the name of the variables
                Note that post is usually autodetected when we have multiple datatasets
                It is mainly used when we have an input file with a single dataset
                so that we can prevent the code from adding "1" to the name of the variables 
                (In this case, indeed, Abinit complains if ndtset=1 is not specified 
                and we don't want ndtset=1 simply because the code will start to add 
                _DS1_ to all the input and output files.
            with_structure: False if section with structure variables should not be printed.
            with_pseudos: False if JSON section with pseudo data should not be added.
        """
        lines = []
        app = lines.append

        if self.comment: app("# " + self.comment.replace("\n", "\n#"))

        post = post if post is not None else ""

        if sortmode is None:
            # no sorting.
            keys = list(self.keys())
        elif sortmode == "a":
            # alphabetical order.
            keys = sorted(list(self.keys()))
        else:
            raise ValueError("Unsupported value for sortmode %s" % str(sortmode))

        with_mnemonics = self.mnemonics
        if with_mnemonics:
            var_database = get_abinit_variables()

        # Extract the items from the dict and add the geo variables at the end
        items = list(self.items())
        if with_structure:
            items.extend(list(self.structure.to_abivars().items()))

        for name, value in items:
            if with_mnemonics:
                v = var_database[name]
                app("# <" + v.definition + ">")

            varname = name + post
            variable = InputVariable(varname, value)
            app(str(variable))

        s = "\n".join(lines)

        if not with_pseudos: return s 

        # Add JSON section with pseudo potentials.
        ppinfo = ["\n\n\n#<JSON>"]
        d = {"pseudos": [p.as_dict() for p in self.pseudos]}
        ppinfo.extend(json.dumps(d, indent=4).splitlines())
        ppinfo.append("</JSON>")
                                                             
        return s + "\n#".join(ppinfo)

    def write(self, filepath):
        """
        Write the input file to file to `filepath`. Returns a string with the input.
        """
        dirname = os.path.dirname(filepath)
        if not os.path.exists(dirname): os.makedirs(dirname)
                                                                                      
        # Write the input file.
        input_string = str(self)
        with open(filepath, "wt") as fh:
            fh.write(input_string)

        return input_string

    @property
    def comment(self):
        try:
            return self._comment
        except AttributeError:
            return None

    def set_comment(self, comment):
        """Set a comment to be included at the top of the file."""
        self._comment = comment

    def set_vars(self, *args, **kwargs):
        """Set the value of the variables provied in the dictionary **kwargs"""
        kwargs.update(dict(*args))
        for varname, varvalue in kwargs.items():
            self[varname] = varvalue
        return kwargs

    def remove_vars(self, keys):
        """Remove the variables listed in keys."""
        values = []
        for key in list_strings(keys):
            if key not in self:
                raise KeyError("key: %s not in self:\n %s" % (key, list(self.keys())))
            values.append(self.pop(key))
        return values

    @property
    def structure(self):
        """The :class:`Structure` associated to this input."""
        return self._structure

    def set_structure(self, structure):
        self._structure = Structure.as_structure(structure)

        # Check volume
        m = self.structure.lattice.matrix
        if np.dot(np.cross(m[0], m[1]), m[2]) <= 0:
            raise self.Error("The triple product of the lattice vector is negative. Use structure abi_sanitize.")

        return self._structure

    # Helper functions to facilitate the specification of several variables.
    def set_kmesh(self, ngkpt, shiftk, kptopt=1):
        """
        Set the variables for the sampling of the BZ.

        Args:
            ngkpt: Monkhorst-Pack divisions
            shiftk: List of shifts.
            kptopt: Option for the generation of the mesh.
        """
        shiftk = np.reshape(shiftk, (-1,3))
        return self.set_vars(ngkpt=ngkpt, kptopt=kptopt, nshiftk=len(shiftk), shiftk=shiftk)

    def set_autokmesh(self, nksmall, kptopt=1):
        """
        Set the variables (ngkpt, shift, kptopt) for the sampling of the BZ.
                                                       
        Args:
            nksmall: Number of k-points used to sample the smallest lattice vector.
            kptopt: Option for the generation of the mesh.
        """
        shiftk = self.structure.calc_shiftk()
        return self.set_vars(ngkpt=self.structure.calc_ngkpt(nksmall), kptopt=kptopt, 
                             nshiftk=len(shiftk), shiftk=shiftk)

    def set_kpath(self, ndivsm, kptbounds=None, iscf=-2):
        """
        Set the variables for the computation of the band structure.

        Args:
            ndivsm: Number of divisions for the smallest segment.
            kptbounds: k-points defining the path in k-space.
                If None, we use the default high-symmetry k-path defined in the pymatgen database.
        """
        if kptbounds is None: kptbounds = self.structure.calc_kptbounds()
        kptbounds = np.reshape(kptbounds, (-1,3))

        return self.set_vars(kptbounds=kptbounds, kptopt=-(len(kptbounds)-1), ndivsm=ndivsm, iscf=iscf)

    def set_kptgw(self, kptgw, bdgw):
        """
        Set the variables (k-points, bands) for the computation of the GW corrections.

        Args
            kptgw: List of k-points in reduced coordinates.
            bdgw: Specifies the range of bands for the GW corrections.
                Accepts iterable that be reshaped to (nkptgw, 2) 
                or a tuple of two integers if the extrema are the same for each k-point.
        """
        kptgw = np.reshape(kptgw, (-1,3))
        nkptgw = len(kptgw)
        if len(bdgw) == 2: bdgw = len(kptgw) * bdgw

        return self.set_vars(kptgw=kptgw, nkptgw=nkptgw, bdgw=np.reshape(bdgw, (nkptgw, 2)))

    @property
    def pseudos(self):
        """List of :class:`Pseudo` objects."""
        return self._pseudos

    @property
    def ispaw(self):
        """True if PAW calculation."""
        return all(p.ispaw for p in self.pseudos)

    @property
    def isnc(self):
        """True if norm-conserving calculation."""
        return all(p.isnc for p in self.pseudos)

    @property
    def num_valence_electrons(self):
        """Number of valence electrons computed from the pseudos and the structure."""
        return self.structure.num_valence_electrons(self.pseudos)

    def linspace(self, varname, start, stop, num=50, endpoint=True):
        """
        Returns `num` evenly spaced samples, calculated over the interval [`start`, `stop`].

        The endpoint of the interval can optionally be excluded.

        Args:
            start: The starting value of the sequence.
            stop: The end value of the sequence, unless `endpoint` is set to False.
                In that case, the sequence consists of all but the last of ``ndtset + 1``
                evenly spaced samples, so that `stop` is excluded.  Note that the step
                size changes when `endpoint` is False.
            num : int, optional
                Number of samples to generate. Default is 50.
            endpoint : bool, optional
                If True, `stop` is the last sample. Otherwise, it is not included.
                Default is True.
        """
        inps = []
        for value in np.linspace(start, stop, num=num, endpoint=endpoint, retstep=False):
            inp = self.deepcopy()
            inp[varname] = value
            inps.append(inp)
        return inps

    def arange(self, varname, start, stop, step):
        """
        Return evenly spaced values within a given interval.

        Values are generated within the half-open interval ``[start, stop)``
        (in other words, the interval including `start` but excluding `stop`).

        When using a non-integer step, such as 0.1, the results will often not
        be consistent.  It is better to use ``linspace`` for these cases.

        Args:
            start:  Start of interval. The interval includes this value. The default start value is 0.
            stop: End of interval.  The interval does not include this value, except
                in some cases where `step` is not an integer and floating point
            step: Spacing between values.  For any output `out`, this is the distance
                between two adjacent values, ``out[i+1] - out[i]``.  The default
                step size is 1.  If `step` is specified, `start` must also be given.
        """
        inps = []
        for value in np.arange(start=start, stop=stop, step=step):
            inp = self.deepcopy()
            inp[varname] = value
            inps.append(inp)
        return inps

    def product(self, *items):
        """
        Cartesian product of input iterables. Equivalent to nested for-loops.

        .. code-block:: python

            inp.product("ngkpt", "tsmear", [[2,2,2], [4,4,4]], [0.1, 0.2, 0.3])
        """
        # Split items into varnames and values
        for i, item in enumerate(items):
            if not is_string(item): break

        varnames, values = items[:i], items[i:]
        if len(varnames) != len(values):
            raise self.Error("The number of variables must equal the number of lists")

        varnames = [ [varnames[i]] * len(values[i]) for i in range(len(values))]
        varnames = itertools.product(*varnames)
        values = itertools.product(*values)

        inps = []
        for names, values in zip(varnames, values):
            inp = self.deepcopy()
            inp.set_vars(**{k: v for k, v in zip(names, values)})
            inps.append(inp)
        return inps

    def new_with_decorators(self, decorators):
        """
        This function receives a list of :class:`AbinitInputDecorator` objects or just a single object,
        applyes the decorators to the input and returns a new :class:`AbinitInput` object.
        self is not changed.
        """
        if not isinstance(decorators, (list, tuple)): decorators = [decorators]

        # Deepcopy only at the first step to improve performance.
        inp = self
        for i, dec in enumerate(decorators):
            inp = dec(inp, deepcopy=(i == 0))

        return inp

    def pycheck(self):
        errors = []
        eapp = errors.append

        m = self.structure.lattice.matrix
        volume = np.dot(np.cross(m[0], m[1]), m[2])
        if volume < 0:
            eapp("The triple product of the lattice vector is negative. Use structure abi_sanitize.")

        #if sel.ispaw and "pawecutdg not in self 
        #if errors: raise self.Error("\n".join(errors))

        return dict2namedtuple(errors=errors, warnings=warnings)
        
    def abivalidate(self):
        """
        Run ABINIT in dry mode to validate the input file.

        Return:
            `namedtuple` with the following attributes:

                retcode: Return code. 0 if OK.
                log_file:  log file of the Abinit run, use log_file.read() to access its content.
                stderr_file: stderr file of the Abinit run. use stderr_file.read() to access its content.

        Raises:
            `RuntimeError` if executable is not in $PATH.
        """
        task = AbinitTask.temp_shell_task(inp=self) 
        retcode = task.start_and_wait(autoparal=False, exec_args=["--dry-run"])
        return dict2namedtuple(retcode=retcode, log_file=task.log_file, stderr_file=task.stderr_file)

    def abiget_ibz(self, ngkpt=None, shiftk=None, kptopt=None, workdir=None, manager=None):
        """
        This function computes the list of points in the IBZ and the corresponding weights.
        It should be called with an input file that contains all the mandatory variables required by ABINIT.

        Args:
            ngkpt: Number of divisions for the k-mesh (default None i.e. use ngkpt from self)
            shiftk: Shiftks (default None i.e. use shiftk from self)
            kptopt: Option for k-point generation. If None, the value in self is used.
            workdir: Working directory of the fake task used to compute the ibz. Use None for temporary dir.
            manager: :class:`TaskManager` of the task. If None, the manager is initialized from the config file.

        Returns:
            `namedtuple` with attributes:
                points: `ndarray` with points in the IBZ in reduced coordinates.
                weights: `ndarray` with weights of the points.
        """
        # Avoid modifications in self.
        inp = self.deepcopy()

        # The magic value that makes ABINIT print the ibz and then stop.
        inp["prtkpt"] = -2

        if ngkpt is not None: inp["ngkpt"] = ngkpt
        if shiftk is not None:
            shiftk = np.reshape(shiftk, (-1,3))
            inp.set_vars(shiftk=shiftk, nshiftk=len(shiftk))

        if kptopt is not None: inp["kptopt"] = kptopt

        # Build a Task to run Abinit in a shell subprocess
        task = AbinitTask.temp_shell_task(inp, workdir=workdir, manager=manager)
        task.start_and_wait(autoparal=False)

        # Read the list of k-points from the netcdf file.
        try:
            with NetcdfReader(os.path.join(task.workdir, "kpts.nc")) as r:
                ibz = collections.namedtuple("ibz", "points weights")
                return ibz(points=r.read_value("reduced_coordinates_of_kpoints"),
                           weights=r.read_value("kpoint_weights"))

        except Exception as exc:
            # Try to understand if it's a problem with the Abinit input.
            report = task.get_event_report()
            if report and report.errors: raise self.Error(str(report))
            raise self.Error("Problem in temp Task executed in %s\n%s" % (task.workdir, exc))

    def abiget_irred_phperts(self, qpt=None, ngkpt=None, shiftk=None, kptopt=None, workdir=None, manager=None):
        """
        This function, computes the list of irreducible perturbations for DFPT.
        It should be called with an input file that contains all the mandatory variables required by ABINIT.

        Args:
            qpt: qpoint of the phonon in reduced coordinates. Used to shift the k-mesh 
                if qpt is not passed, self must already contain "qpt" otherwise an exception is raised.
            ngkpt: Number of divisions for the k-mesh (default None i.e. use ngkpt from self)
            shiftk: Shiftks (default None i.e. use shiftk from self)
            kptopt: Option for k-point generation. If None, the value in self is used.
            workdir: Working directory of the fake task used to compute the ibz. Use None for temporary dir.
            manager: :class:`TaskManager` of the task. If None, the manager is initialized from the config file.

        Returns:
            List of dictionaries with the Abinit variables defining the irreducible perturbation
            Example:

                [{'idir': 1, 'ipert': 1, 'qpt': [0.25, 0.0, 0.0]},
                 {'idir': 2, 'ipert': 1, 'qpt': [0.25, 0.0, 0.0]}]

        """
        # Avoid modifications in self.
        inp = self.deepcopy()

        qpt = inp.get("qpt") if qpt is None else qpt
        if qpt is None:
            raise ValueError("qpt is not in the input and therefore it must be passed explicitly")

        if ngkpt is not None: inp["ngkpt"] = ngkpt
        if shiftk is not None:
            shiftk = np.reshape(shiftk, (-1,3))
            inp.set_vars(shiftk=shiftk, nshiftk=len(inp.shiftk))
                                                                 
        if kptopt is not None: inp["kptopt"] = kptopt

        inp.set_vars(
            rfphon=1,                         # Will consider phonon-type perturbation
            nqpt=1,                           # One wavevector is to be considered
            qpt=qpt,                          # q-wavevector.
            rfatpol=[1, len(inp.structure)],  # Set of atoms to displace.
            rfdir=[1, 1, 1],                  # Along this set of reduced coordinate axis.
            paral_rf=-1,                      # Magic value to get the list of irreducible perturbations for this q-point.
        )

        # Build a Task to run Abinit in a shell subprocess
        task = AbinitTask.temp_shell_task(inp, workdir=workdir, manager=manager)
        task.start_and_wait(autoparal=False)

        # Parse the file to get the perturbations.
        try:
            return yaml_read_irred_perts(task.log_file.path)
        except Exception as exc:
            # Try to understand if it's a problem with the Abinit input.
            report = task.get_event_report()
            if report and report.errors: raise self.Error(str(report))
            raise self.Error("Problem in temp Task executed in %s\n%s" % (task.workdir, exc))

    def abiget_autoparal_pconfs(self, max_ncpus, autoparal=1, workdir=None, manager=None):
        """Get all the possible configurations up to max_ncpus"""
        inp = self.deepcopy()
        inp.set_vars(autoparal=autoparal, max_ncpus=max_ncpus)

        # Run the job in a shell subprocess with mpi_procs = 1
        # Return code is always != 0 
        task = AbinitTask.temp_shell_task(inp, workdir=workdir, manager=manager)
        task.start_and_wait(autoparal=False)

        ##############################################################
        # Parse the autoparal configurations from the main output file
        ##############################################################
        parser = ParalHintsParser()
        try:
            pconfs = parser.parse(task.output_file.path)
            return pconfs
        except parser.Error:
            # Try to understand if it's a problem with the Abinit input.
            report = task.get_event_report()
            if report and report.errors: raise self.Error(str(report))
            raise self.Error("Problem in temp Task executed in %s\n%s" % (task.workdir, exc))


class MultiDataset(object):
    """
    This object is essentially a list of :class:`AbinitInput objects.
    that provides an easy-to-use interface to apply global changes to the 
    the inputs stored in the objects.

    Let's assume for example that multi contains two AbinitInput object and we
    want to set `ecut` to 1 in both dictionaries. The direct approach would be:

        for inp in multi:
            inp.set_vars(ecut=1)

    or alternatively:

        for i in range(multi.ndtset):
            multi[i].set_vars(ecut=1)


    MultiDataset provides its own implementaion of __getattr__ so that one simply use:

         multi.set_vars(ecut=1)

    .. warning::

        MultiDataset does not support calculations done with different sets of pseudopotentials.
        The inputs can have different crystalline structures (as long as the atom types are equal)
        but each input in MultiDataset must have the same set of pseudopotentials.
    """
    Error = AbinitInputError

    @classmethod
    def from_inputs(cls, inputs):
        for inp in inputs:
            if any(p1 != p2 for p1, p2 in zip(inputs[0].pseudos, inp.pseudos)):
                raise ValueError("Pseudos must be consistent when from_inputs is invoked.")

        # Build MultiDataset from input structures and pseudos and add inputs.
        multi = cls(structure=[inp.structure for inp in inputs], pseudos=inputs[0].pseudos, ndtset=len(inputs))

        for inp, new_inp in zip(inputs, multi):
            new_inp.set_vars(**inp)

        return multi

    def __init__(self, structure, pseudos, pseudo_dir="", ndtset=1):
        """
        Args:
            structure: file with the structure, :class:`Structure` object or dictionary with ABINIT geo variable
                Accepts also list of objects that can be converted to Structure object.
                In this case, however, ndtset must be equal to the length of the list.
            pseudos: String or list of string with the name of the pseudopotential files.
            pseudo_dir: Name of the directory where the pseudopotential files are located.
            ndtset: Number of datasets.
        """
        # Setup of the pseudopotential files.
        if isinstance(pseudos, PseudoTable):
            pseudos = pseudos

        elif all(isinstance(p, Pseudo) for p in pseudos):
            pseudos = PseudoTable(pseudos)

        else:
            # String(s)
            pseudo_dir = os.path.abspath(pseudo_dir)
            pseudo_paths = [os.path.join(pseudo_dir, p) for p in list_strings(pseudos)]

            missing = [p for p in pseudo_paths if not os.path.exists(p)]
            if missing:
                raise self.Error("Cannot find the following pseudopotential files:\n%s" % str(missing)) 

            pseudos = PseudoTable(pseudo_paths)

        # Build the list of AbinitInput objects.
        if ndtset <= 0:
            raise ValueError("ndtset %d cannot be <=0" % ndtset)

        if not isinstance(structure, (list, tuple)):
            self._inputs = [AbinitInput(structure=structure, pseudos=pseudos) for i in range(ndtset)]
        else:
            assert len(structure) == ndtset
            self._inputs = [AbinitInput(structure=s, pseudos=pseudos) for s in structure]

        # Check pseudos
        #for i in range(self.ndtset):
        #    if any(p1 != p2 for p1, p2 in zip(self[0].pseudos, self[i].pseudos)):
        #        raise selfError("Pseudos must be consistent when from_inputs is invoked.")

    @property
    def ndtset(self):
        """Number of inputs in self."""
        return len(self)

    @property
    def pseudos(self):
        return self[0].pseudos

    @property
    def ispaw(self):
        """True if PAW calculation."""
        return all(p.ispaw for p in self.pseudos)

    @property
    def isnc(self):
        """True if norm-conserving calculation."""
        return all(p.isnc for p in self.pseudos)

    def __len__(self):
        return len(self._inputs)

    def __getitem__(self, key):
        return self._inputs[key]

    def __iter__(self):
        return self._inputs.__iter__()

    def __getattr__(self, name):
        #print("in getname with name: %s" % name)
        m = getattr(self._inputs[0], name)
        if m is None:
            raise AttributeError("Cannot find attribute %s in AbinitInput object" % name)
        isattr = not callable(m)

        def on_all(*args, **kwargs):
            results = []
            for obj in self._inputs:
                a = getattr(obj, name)
                #print("name", name, ", type:", type(a), "callable: ",callable(a))
                if callable(a):
                    results.append(a(*args, **kwargs))
                else:
                    results.append(a)

            return results

        if isattr: on_all = on_all()
        return on_all

    def append(self, abinit_input):
        """Add an :class:`AbinitInput` to the list."""
        assert isinstance(abinit_input, AbinitInput)
        self._inputs.append(abinit_input)

    def extend(self, abinit_inputs):
        """Extends self with a list of :class:`AbinitInput` objects."""
        assert all(isinstance(inp, AbinitInput) for inp in abinit_inputs)
        self._inputs.extend(abinit_inputs)

    def addnew_from(self, dtindex):
        self.append(self[dtindex].deepcopy())

    def split_datasets(self):
        return self._inputs

    def deepcopy(self):
        """Deep copy of the object."""
        return copy.deepcopy(self)

    @property
    def has_same_structures(self):
        return all(self[0].structure == inp.structure for inp in self)

    def __str__(self):
        """String representation i.e. the input file read by Abinit."""
        if self.ndtset > 1:
            # Multi dataset mode.
            lines = ["ndtset %d" % self.ndtset]

            #same_structures = self.has_same_structures

            for i, inp in enumerate(self):
                header = "### DATASET %d ###" % (i + 1)
                is_last = (i==self.ndtset - 1)
                #with_structure = True 
                #if same_structure and not is_last: with_structure = False

                s = inp.to_string(post=str(i+1), with_pseudos=is_last)
                if s:
                    header = len(header) * "#" + "\n" + header + "\n" + len(header) * "#" + "\n"
                    s = "\n" + header + s + "\n"

                lines.append(s)

            return "\n".join(lines)

        else:
            # single datasets ==> don't append the dataset index to the variables.
            # this trick is needed because Abinit complains if ndtset is not specified 
            # and we have variables that end with the dataset index e.g. acell1
            # We don't want to specify ndtset here since abinit will start to add DS# to 
            # the input and output files thus complicating the algorithms we have to use to locate the files.
            return self[0].to_string()

    #def __dir__(self):
    #    """Interactive prompt"""
    #    #return dir(self) + dir(self._inputs[0])
    #    return dir(self._inputs[0])


class AnaddbInputError(Exception):
    """Base error class for exceptions raised by `AnaddbInput`"""


class AnaddbInput(MutableMapping, Has_Structure):

    Error = AnaddbInputError

    def __init__(self, structure, comment="", vars=None):
        """
        Args:
            structure: :class:`Structure` object 
            comment: Optional string with a comment that will be placed at the beginning of the file.
            vars: Dictionary with Anaddb input variables (default: empty)
        """
        self._structure = structure
        self.comment = comment

        vars = {} if vars is None else vars
        for key in vars:
            self._check_varname(key)

        self._vars = OrderedDict(**vars)

    def _check_varname(self, key):
        if not is_anaddb_var(key):
            raise self.Error("%s is not a registered Anaddb variable\n"
                             "If you are sure the name is correct, please contact the abipy developers\n" 
                             "or modify the JSON file abipy/abio/anaddb_vars.json" % key)

    @classmethod
    def modes_at_qpoint(cls, structure, qpoint, asr=2, chneut=1, dipdip=1, vars=None):
        """
        Input file for the calculation of the phonon frequencies at a given q-point.

        Args:
            Structure: :class:`Structure` object
            qpoint: Reduced coordinates of the q-point where phonon frequencies and modes are wanted
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            vars: Dictionary with extra Anaddb input variables (default: empty)
        """
        new = cls(structure, comment="ANADB input for phonon frequencies at one q-point", vars=vars)

        new.set_vars(
            ifcflag=1,        # Interatomic force constant flag
            asr=asr,          # Acoustic Sum Rule
            chneut=chneut,    # Charge neutrality requirement for effective charges.
            dipdip=dipdip,    # Dipole-dipole interaction treatment
            # This part if fixed
            ngqpt=(1, 1,  1), 
            nqshft=1,         
            q1shft=qpoint,
            nqpath=2,
            qpath=list(qpoint) + [0, 0, 0],
            ndivsm=1
        )

        return new

    #@classmethod
    #def phbands(cls, structure, ngqpt, nqsmall, q1shft=(0,0,0),
    #          asr=2, chneut=0, dipdip=1, dos_method="tetra", **kwargs):
    #    """
    #    Build an anaddb input file for the computation of phonon band structure.
    #    """

    #@classmethod
    #def phdos(cls, structure, ngqpt, nqsmall, q1shft=(0,0,0),
    #          asr=2, chneut=0, dipdip=1, dos_method="tetra", **kwargs):
    #    """
    #    Build an anaddb input file for the computation of phonon DOS.
    #    """

    @classmethod
    def phbands_and_dos(cls, structure, ngqpt, nqsmall, ndivsm=20, q1shft=(0,0,0),
                        qptbounds=None, asr=2, chneut=0, dipdip=1, dos_method="tetra", vars=None):
        """
        Build an anaddb input file for the computation of phonon bands and phonon DOS.

        Args:
            structure: :class:`Structure` object
            ngqpt: Monkhorst-Pack divisions for the phonon Q-mesh (coarse one)
            nqsmall: Used to generate the (dense) mesh for the DOS.
                It defines the number of q-points used to sample the smallest lattice vector.
            ndivsm: Used to generate a normalized path for the phonon bands.
                If gives the number of divisions for the smallest segment of the path.
            q1shft: Shifts used for the coarse Q-mesh
            qptbounds Boundaries of the path. If None, the path is generated from an internal database
                depending on the input structure.
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            dos_method: Possible choices: "tetra", "gaussian" or "gaussian:0.001 eV".
                In the later case, the value 0.001 eV is used as gaussian broadening
            vars: Dictionary with extra Anaddb input variables (default: empty)
        """
        dosdeltae, dossmear = None, None

        if dos_method == "tetra":
            prtdos = 2
        elif "gaussian" in dos_method:
            prtdos = 1
            i = dos_method.find(":")
            if i != -1:
                value, eunit = dos_method[i+1:].split()
                dossmear = Energy(float(value), eunit).to("Ha")
        else:
            raise cls.Error("Wrong value for dos_method: %s" % dos_method)

        new = cls(structure, comment="ANADB input for phonon bands and DOS", vars=vars)

        # Parameters for the dos.
        new.set_autoqmesh(nqsmall)
        new.set_vars(
            prtdos=prtdos,
            dosdeltae=dosdeltae,
            dossmear=dossmear,
        )

        new.set_qpath(ndivsm, qptbounds=qptbounds)
        q1shft = np.reshape(q1shft, (-1, 3))

        new.set_vars(
            ifcflag=1,
            ngqpt=np.array(ngqpt),
            q1shft=q1shft,
            nqshft=len(q1shft),
            asr=asr,
            chneut=chneut,
            dipdip=dipdip,
        )

        return new

    @classmethod
    def thermo(cls, structure, ngqpt, nqsmall, q1shft=(0, 0, 0), nchan=1250, nwchan=5, thmtol=0.5,
               ntemper=199, temperinc=5, tempermin=5., asr=2, chneut=1, dipdip=1, ngrids=10, vars=None):
        """
        Build an anaddb input file for the computation of phonon bands and phonon DOS.

        Args:
            structure: :class:`Structure` object
            ngqpt: Monkhorst-Pack divisions for the phonon Q-mesh (coarse one)
            nqsmall: Used to generate the (dense) mesh for the DOS.
                It defines the number of q-points used to sample the smallest lattice vector.
            q1shft: Shifts used for the coarse Q-mesh
            nchan:
            nwchan:
            thmtol:
            ntemper:
            temperinc:
            tempermin:
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            ngrids:
            vars: Dictionary with extra Anaddb input variables (default: empty)

            #!Flags
            # ifcflag   1     ! Interatomic force constant flag
            # thmflag   1     ! Thermodynamical properties flag
            #!Wavevector grid number 1 (coarse grid, from DDB)
            #  brav    2      ! Bravais Lattice : 1-S.C., 2-F.C., 3-B.C., 4-Hex.)
            #  ngqpt   4  4  4   ! Monkhorst-Pack indices
            #  nqshft  1         ! number of q-points in repeated basic q-cell
            #  q1shft  3*0.0
            #!Effective charges
            #     asr   1     ! Acoustic Sum Rule. 1 => imposed asymetrically
            #  chneut   1     ! Charge neutrality requirement for effective charges.
            #!Interatomic force constant info
            #  dipdip  1      ! Dipole-dipole interaction treatment
            #!Wavevector grid number 2 (series of fine grids, extrapolated from interat forces)
            #  ng2qpt   20 20 20  ! sample the BZ up to ngqpt2
            #  ngrids   5         ! number of grids of increasing size#  q2shft   3*0.0
            #!Thermal information
            #  nchan   1250   ! # of channels for the DOS with channel width 1 cm-1
            #  nwchan  5      ! # of different channel widths from this integer down to 1 cm-1
            #  thmtol  0.120  ! Tolerance on thermodynamical function fluctuations
            #  ntemper 10     ! Number of temperatures
            #  temperinc 20.  ! Increment of temperature in K for temperature dependency
            #  tempermin 20.  ! Minimal temperature in Kelvin
            # This line added when defaults were changed (v5.3) to keep the previous, old behaviour
            #  symdynmat 0

        """
        new = cls(structure, comment="ANADB input for thermodynamics", vars=vars)
        new.set_autoqmesh(nqsmall)

        q1shft = np.reshape(q1shft, (-1, 3))

        new.set_vars(
            ifcflag=1,
            thmflag=1,
            ngqpt=np.array(ngqpt),
            ngrids=ngrids,
            q1shft=q1shft,
            nqshft=len(q1shft),
            asr=asr,
            chneut=chneut,
            dipdip=dipdip,
            nchan=nchan,
            nwchan=nwchan,
            thmtol=thmtol,
            ntemper=ntemper,
            temperinc=temperinc,
            tempermin=tempermin,
        )

        return new

    @classmethod
    def modes(cls, structure, enunit=2, asr=2, chneut=1, vars=None):
        """
        Build an anaddb input file for the computation of phonon modes.

        Args:
            Structure: :class:`Structure` object
            ngqpt: Monkhorst-Pack divisions for the phonon Q-mesh (coarse one)
            nqsmall: Used to generate the (dense) mesh for the DOS.
                It defines the number of q-points used to sample the smallest lattice vector.
            q1shft: Shifts used for the coarse Q-mesh
            qptbounds Boundaries of the path. If None, the path is generated from an internal database
                depending on the input structure.
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            vars: Dictionary with extra Anaddb input variables (default: empty)

        #!General information
        #enunit    2
        #eivec     1
        #!Flags
        #dieflag   1
        #ifcflag   1
        #ngqpt     1 1 1
        #!Effective charges
        #asr       2
        #chneut    2
        # Wavevector list number 1
        #nph1l     1
        #qph1l   0.0  0.0  0.0    1.0   ! (Gamma point)
        #!Wavevector list number 2
        #nph2l     3      ! number of phonons in list 1
        #qph2l   1.0  0.0  0.0    0.0
        #        0.0  1.0  0.0    0.0
        #        0.0  0.0  1.0    0.0
        """
        new = cls(structure, comment="ANADB input for modes", vars=vars)

        new.set_vars(
            enunit=enunit,
            eivec=1,
            ifcflag=1,
            dieflag=1,
            ngqpt=[1.0, 1.0, 1.0],
            asr=asr,
            chneut=chneut,
            nph1l=1,
            qph1l=[0.0, 0.0, 0.0, 1.0],
            nph2l=3,
            qph2l=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]
        )

        return new

    # ABC protocol: __delitem__, __getitem__, __iter__, __len__, __setitem__
    def __delitem__(self, key):
        return self._vars.__delitem__(key)
        
    def __getitem__(self, key):
        return self._vars.__getitem__(key)

    def __iter__(self):
        return self._vars.__iter__()

    def __len__(self):
        return len(self._vars)

    def __setitem__(self, key, value):
        self._check_varname(key)
        return self._vars.__setitem__(key, value)

    @property
    def structure(self):
        return self._structure

    def __repr__(self):
        return "<%s at %s>" % (self.__class__.__name__, id(self))

    def __str__(self):
        return self.to_string()

    def make_input(self):
        return self.to_string()

    def to_string(self, sortmode=None):
        """
        String representation.

        Args:
            sortmode: "a" for alphabetical order, None if no sorting is wanted
        """
        lines = []
        app = lines.append

        if self.comment:
            app("# " + self.comment.replace("\n", "\n#"))

        if sortmode is None:
            # no sorting.
            keys = self.keys()
        elif sortmode == "a":
            # alphabetical order.
            keys = sorted(self.keys())
        else:
            raise ValueError("Unsupported value for sortmode %s" % str(sortmode))

        for varname in keys:
            value = self[varname]
            app(str(InputVariable(varname, value)))

        return "\n".join(lines)

    def deepcopy(self):
        """Deep copy of the input."""
        return copy.deepcopy(self)

    def set_vars(self, *args, **kwargs):
        """Set the value of the variables"""
        kwargs.update(dict(*args))
        for varname, varvalue in kwargs.items():
            self[varname] = varvalue
        return kwargs

    def set_qpath(self, ndivsm, qptbounds=None):
        """
        Set the variables for the computation of the phonon band structure.

        Args:
            ndivsm: Number of divisions for the smallest segment.
            qptbounds: q-points defining the path in k-space.
                If None, we use the default high-symmetry k-path defined in the pymatgen database.
        """
        if qptbounds is None: qptbounds = self.structure.calc_kptbounds()
        qptbounds = np.reshape(qptbounds, (-1, 3))

        return self.set_vars(ndivsm=ndivsm, nqpath=len(qptbounds), qpath=qptbounds)

    def set_autoqmesh(self, nqsmall):
        """
        Set the variable nqpt for the sampling of the BZ.

        Args:
            nqsmall: Number of divisions used to sample the smallest lattice vector.
        """
        return self.set_vars(ng2qpt=self.structure.calc_ngkpt(nqsmall))