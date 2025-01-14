import numpy as np
import pickle
from string import Template


def generate_rand_eci_vec(num_eci: int, stdev: float, normalization: float):
    """Generates a random, normalized vector in eci space. Each element is drawn from a standard normal distribution.

    Parameters
    ----------
    num_eci : int
        The number of ECI.
    stdev : float
        Standard deviation defining the standard normal distribution for each element.
    normalization : float
        Magnitude to scale the random vector by.

    Returns
    -------
    eci_vec : numpy.ndarray
        Random, scaled vector in ECI space.
    """
    eci_vec = np.random.normal(scale=stdev, size=num_eci)
    eci_vec = (eci_vec / np.linalg.norm(eci_vec)) * normalization
    return eci_vec


def metropolis_hastings_ratio(
    current_eci: np.ndarray,
    proposed_eci: np.ndarray,
    current_energy: np.ndarray,
    proposed_energy: np.ndarray,
    formation_energy: np.ndarray,
):
    """Acceptance probability ratio defined in Zabaras et. al, https://doi.org/10.1016/j.cpc.2014.07.013. First part of equation (12)
    Parameters
    ----------
    current_eci : numpy.ndarray shape(number_eci)
        Vector of current ECI values.
    proposed_eci : numpy.ndarray shape(number_eci)
        Vector of proposed eci values, differing from current_eci by a random vector in ECI space.
    current_energy : numpy.ndarray, shape(number_dft_computed_configs_)
        Energy calculated with current_eci.
    proposed_energy : numpy.ndarray, shape(number_dft_computed_configs_)
        Energy calculated using proposed_eci.
    formation_energy : numpy.ndarray, shape(number_dft_computed_configs_)

    Returns
    -------
    mh_ratio : float
        Ratio defined in paper listed above- used in deciding whether to accept or reject proposed_eci.
    """

    left_term = np.power(
        (np.linalg.norm(proposed_eci, ord=1) / np.linalg.norm(current_eci, ord=1)),
        (-1 * current_eci.shape[0]),
    )

    right_term_numerator = np.linalg.norm(formation_energy - proposed_energy)
    right_term_denom = np.linalg.norm(formation_energy - current_energy)

    right_term = np.power(
        (right_term_numerator / right_term_denom), (-1 * formation_energy.shape[0])
    )

    mh_ratio = left_term * right_term
    if mh_ratio > 1:
        mh_ratio = 1
    return mh_ratio


def run_eci_monte_carlo(
    corr_comp_energy_file: str,
    eci_walk_step_size: float,
    iterations: int,
    sample_frequency: int,
    burn_in=1000000,
    output_file_path=False,
):
    """Samples ECI space according to Metropolis Monte Carlo, recording ECI values and most likely ground state configurations.

    Parameters
    ----------
    corr_comp_energy_file : str
        Path to json casm query output file (output of: "casm query -k corr comp formation_energy -j -o filename.json")
    eci_walk_step_size : int
        Magnitude of the random step vector in ECI space. (Try for a value that gives an acceptance rate of ~ 0.24)
    iterations : int
        Number of steps to perform in the monte carlo algorithm.
    sample_frequency : int
        The number of steps that pass before ECI and proposed ground states are recorded.
    burn_in : int
        The number of steps to "throw away" before ECI and proposed ground states are recorded.
    output_dir : str
        Path to the directory where monte carlo results should be written. By default, results are not written to a file.

    Returns
    -------
    results : dict

        "iterations": int
            Number of monte carlo iterations, including burn in.
        "sample_frequency": int
            Frequency to sample and write the monte carlo results (current ECI, hulldist, proposed ground states, etc). Total number of samples = (iterations-burn_in)/sample_frequency
        "burn_in": int
            Number of iterations to "throw away" before recording samples.
        "sampled_eci": numpy.ndarray shape(number_samples, number_eci)
            Each row contains the eci values of a given iteration.
        "acceptance": numpy.ndarray
            Vector of booleans signifying whether a proposed step in ECI space was accepted or rejected.
        "acceptance_prob": float
            Fraction of proposed steps that were accepted over the total number of steps.
        "proposed_ground_states_indices": numpy.ndarray
            Vector of configuration indices describing which configurations were "flagged" as potential ground states.
        "rms": numpy.ndarray
            Vector of RMSE values for each iteration.
        "names": list
            List of configuration names, in the order which they are written in the provided casm query data file.
        "lasso_eci": numpy.ndarray shape(number_ecis)
            Vector of ECI values decided by the initial LASSOCV regression.

        sampled_eci : numpy.ndarray
            Matrix of recorded ECI. M rows of sampled ECI, where M = (Number of iterations / sample frequency). Each row is a set of N ECI, where N is the number of correlations.
        acceptance : numpy.ndarray
            Vector of booleans dictating whether a step was accepted (True) or rejected (False)
        acceptance_prob : float
            Number of accepted steps divided by number of total steps.
        proposed_ground_state_indices : numpy.ndarray
            Vector of indices denoting configurations which appeared below the DFT hull across all of the Monte Carlo steps.
        rms : numpy.ndarray
            Root Mean Squared Error of the calculated energy vs DFT energy for each Monte Carlo step.
        names : list
            List of configuraton names used in the Monte Carlo calculations.
    """
    # Read data from casm query json output
    data = vu.casm_query_reader(corr_comp_energy_file)
    corr = data["corr"]
    formation_energy = data["formation_energy"]
    comp = data["comp"]

    # Dealing with compatibility: Different descriptors for un-calculated formation energy (1.1.2->{}, 1.2-> null (i.e. None))
    uncalculated_energy_descriptor = None
    if {} in formation_energy:
        uncalculated_energy_descriptor = {}

    # downsampling only the calculated configs:
    downsample_selection = formation_energy != uncalculated_energy_descriptor
    corr_calculated = corr[downsample_selection]
    formation_energy_calculated = formation_energy[downsample_selection]
    comp_calculated = comp[downsample_selection]

    # Find and store the DFT hull:
    points = np.zeros(
        (formation_energy_calculated.shape[0], comp_calculated.shape[1] + 1)
    )
    points[:, 0:-1] = comp_calculated
    points[:, -1] = formation_energy_calculated
    hull = ConvexHull(points)
    dft_hull_simplices, dft_hull_config_indices = lower_hull(hull, energy_index=-2)
    dft_hull_corr = corr_calculated[dft_hull_config_indices]
    dft_hull_vertices = hull.points[dft_hull_config_indices]

    # Run lassoCV to get expected eci values
    lasso_eci = run_lassocv(corr_calculated, formation_energy_calculated)

    # Instantiate lists
    acceptance = []
    rms = []
    sampled_eci = []
    proposed_ground_states_indices = np.array([])
    sampled_hulldist = []

    # Perform MH Monte Carlo
    current_eci = lasso_eci
    sampled_eci.append(current_eci)
    for i in tqdm(range(iterations), desc="Monte Carlo Progress"):
        eci_random_vec = generate_rand_eci_vec(
            num_eci=lasso_eci.shape[0], stdev=1, normalization=eci_walk_step_size
        )
        proposed_eci = current_eci + eci_random_vec

        current_energy = np.matmul(corr_calculated, current_eci)
        proposed_energy = np.matmul(corr_calculated, proposed_eci)

        mh_ratio = metropolis_hastings_ratio(
            current_eci,
            proposed_eci,
            current_energy,
            proposed_energy,
            formation_energy_calculated,
        )

        acceptance_comparison = np.random.uniform()
        if mh_ratio >= acceptance_comparison:
            acceptance.append(True)
            current_eci = proposed_eci
            energy_for_error = proposed_energy
        else:
            acceptance.append(False)
            energy_for_error = current_energy

        # Calculate and append rms:
        mse = mean_squared_error(formation_energy_calculated, energy_for_error)
        rms.append(np.sqrt(mse))

        # Compare to DFT hull
        full_predicted_energy = np.matmul(corr, current_eci)
        dft_hull_clex_predict_energies = np.matmul(dft_hull_corr, current_eci)
        hulldist = checkhull(
            dft_hull_vertices[:, 0:-1],
            dft_hull_clex_predict_energies,
            comp,
            full_predicted_energy,
        )
        sampled_hulldist.append(hulldist)
        below_hull_selection = hulldist < 0
        below_hull_indices = np.ravel(np.array(below_hull_selection.nonzero()))

        # Only record a subset of all monte carlo steps to avoid excessive correlation
        if (i > burn_in) and (i % sample_frequency == 0):
            sampled_eci.append(current_eci)
            proposed_ground_states_indices = np.concatenate(
                (proposed_ground_states_indices, below_hull_indices)
            )

    acceptance = np.array(acceptance)
    sampled_eci = np.array(sampled_eci)
    acceptance_prob = np.count_nonzero(acceptance) / acceptance.shape[0]

    results = {
        "iterations": iterations,
        "sample_frequency": sample_frequency,
        "burn_in": burn_in,
        "sampled_eci": sampled_eci,
        "acceptance": acceptance,
        "acceptance_prob": acceptance_prob,
        "proposed_ground_states_indices": proposed_ground_states_indices,
        "rms": rms,
        "names": data["names"],
        "lasso_eci": lasso_eci,
        # "sampled_hulldist": sampled_hulldist,
    }
    if output_file_path:
        print("Saving results to %s" % output_file_path)
        with open(output_file_path, "wb") as f:
            pickle.dump(results, f, protocol=pickle.HIGHEST_PROTOCOL)

    return results


def format_stan_model(
    eci_variance_args,
    likelihood_variance_args,
    eci_prior="normal",
    eci_variance_prior="gamma",
    likelihood_variance_prior="gamma",
    fixed_variance=False,
) -> str:
    """
    Parameters
    ----------
    eci_variance_args: tuple
        if fixed_variance is True, this should be a single float for eci variance. eg. eci_variance_args = 0.0016
        if fixed_variance is False, give arguments for the distribution as a tuple. eg. eci_variance_args = (1,1)
    likelihood_variance_args: tuple
        if fixed_variance is True, this should be a single float for the model variance. eg. likelihood_variance_args = 0.005
        if fixed_variance is False, give arguments for the distribution as a tuple. eg. eci_variance_args = (1,1)
    eci_prior: string
        Distribution type for ECI priors
    eci_variance_prior: string
        Distribution type for ECI variance prior
    likelihood_variance_prior: string
        Distribution type for model variance prior
    fixed_variance: Bool
        If True, model and ECI variance are fixed values. If false, they follow a distribution governed by hyperparameters.
        This choice will affect the eci and likelihood variance arg inputs; please read documentation for both.

    Returns
    -------
    model_template : str
        Formatted stan model template
    """

    # Old args:
    # TODO: Add filter on string arguments

    supported_eci_priors = ["normal"]
    supported_eci_variance_priors = ["gamma"]
    supported_model_variance_priors = ["gamma"]

    assert eci_prior in supported_eci_priors, "Specified ECI prior is not suported."
    assert (
        eci_variance_prior in supported_eci_variance_priors
    ), "Specified ECI variance prior is not supported."

    assert (
        likelihood_variance_prior in supported_model_variance_priors
    ), "Specified model variance prior is not supported."
    # TODO: make this a single string that can be modified to allow any combination of fixed / non-fixed ECI and model variance priors.

    if fixed_variance:
        # If model and ECI variance are fixed to scalar values
        formatted_sigma = str(likelihood_variance_args)
        formatted_eci_variance = str(eci_variance_args)

        ce_model = Template(
            """data {
        int K; 
        int n_configs;
        matrix[n_configs, K] corr;
        vector[n_configs] energies;
    }
parameters {
        vector[K] eci;
    }
model 
    {
        real sigma = $formatted_sigma;
        for (k in 1:K){
            eci[k] ~ normal(0,$formatted_eci_variance);
        }
        energies ~ normal(corr * eci, sigma);
    }"""
        )
    else:
        # If model and ECI variance are not fixed (follows a distribution)
        formatted_sigma = str(likelihood_variance_args)
        formatted_eci_variance = eci_variance_prior + str(eci_variance_args)
        ce_model = Template(
            """data {
        int K; 
        int n_configs;
        matrix[n_configs, K] corr;
        vector[n_configs] energies;
    }
parameters {
        vector[K] eci;
        vector<lower=0>[K] eci_variance;
    }
model 
    {
        real sigma = $formatted_sigma;
        for (k in 1:K){
            eci_variance[k] ~ $formatted_eci_variance ;
            eci[k] ~ normal(0,eci_variance[k]);
        }
        energies ~ normal(corr * eci, sigma);
    }"""
        )
    model_template = ce_model.substitute(
        formatted_sigma=formatted_sigma, formatted_eci_variance=formatted_eci_variance,
    )
    # model_template = ce_model.substitute(formatted_eci_variance=formatted_eci_variance)
    return model_template


def cross_validate_stan_model(
    data_file: str,
    num_samples: int,
    eci_variance_args,
    likelihood_variance_args,
    cross_val_directory: str,
    random_seed=5,
    eci_prior="normal",
    eci_variance_prior="gamma",
    likelihood_variance_prior="gamma",
    stan_model_file="stan_model.txt",
    eci_output_file="results.pkl",
    energy_tag="formation_energy",
    num_chains=4,
    kfold=5,
    submit_with_slurm=True,
    fixed_variance=False,
):
    """Perform kfold cross validation on a specific stan model. Wraps around format_stan_model() and format_stan_executable_script().

    Parameters:
    -----------
    data_file: string
        Path to casm query output containing correlations, compositions and formation energies
    num_samples: int
        Number of samples in the stan monte carlo process
    eci_variance_args: tuple
        arguments for gamma distribution as a tuple. eg. eci_variance_args = (1,1)
    cross_val_directory: str
        Path to directory where the kfold cross validation runs will write data.
    random_seed: int
        Random number seed for randomized kfold data splitting. Providing the same seed will result in identical training / testing data partitions.
    eci_prior: string
        Distribution type for ECI priors
    eci_variance_prior: string
        Distribution type for ECI variance prior
    stan_model_file: string
        Path to text file containing stan model specifics
    eci_output_file: string
        Path to file where Stan will write the sampled ECI
    energy_tag: string
        Tag for the energy column in the casm query output (Can be formation_energy, energy, energy_per_atom, formation_energy_per_atom, etc.)
    num_chains: int
        Number of simultaneous markov chains
    kfold: int
        Number of "bins" to split training data into.
    submit_with_slurm: bool
        Decides if the function will submit with slurm. Defaults to true.

    Returns:
    --------
    None
    """
    # create directory for kfold cross validation
    os.makedirs(cross_val_directory, exist_ok=True)

    # load data
    with open(data_file) as f:
        data = np.array(json.load(f))

    # setup kfold batches, format for stan input
    data_length = data.shape[0]
    ss = ShuffleSplit(n_splits=kfold, random_state=random_seed)
    indices = range(data_length)

    count = 0
    for train_index, test_index in ss.split(indices):

        # make run directory for this iteration of the kfold cross validation
        this_run_path = os.path.join(cross_val_directory, "crossval_" + str(count))
        os.makedirs(this_run_path, exist_ok=True)

        # slice data; write training and testing data in separate files.
        training_data = data[train_index].tolist()
        testing_data = data[test_index].tolist()
        training_data_path = os.path.join(this_run_path, "training_data.json")
        with open(training_data_path, "w") as f:
            json.dump(training_data, f)
        with open(os.path.join(this_run_path, "testing_data.json"), "w") as f:
            json.dump(testing_data, f)

        # Also write training/ testing indices for easier post processing.
        run_info = {
            "training_set": train_index.tolist(),
            "test_set": test_index.tolist(),
            "eci_variance_args": eci_variance_args,
            "likelihood_variance_args": likelihood_variance_args,
            "data_source": data_file,
            "random_seed": random_seed,
        }
        with open(os.path.join(this_run_path, "run_info.json"), "w") as f:
            json.dump(run_info, f)

        # Write model info

        # format and write stan model
        formatted_stan_model = format_stan_model(
            eci_variance_args=eci_variance_args,
            eci_prior=eci_prior,
            eci_variance_prior=eci_variance_prior,
            likelihood_variance_args=likelihood_variance_args,
            fixed_variance=fixed_variance,
        )
        with open(os.path.join(this_run_path, stan_model_file), "w") as f:
            f.write(formatted_stan_model)

        # format and write stan executable python script
        formatted_stan_script = format_stan_executable_script(
            data_file=training_data_path,
            stan_model_file=stan_model_file,
            eci_output_file=eci_output_file,
            num_samples=num_samples,
            energy_tag=energy_tag,
            num_chains=4,
        )

        with open(os.path.join(this_run_path, "run_stan.py"), "w") as f:
            f.write(formatted_stan_script)

        # format and write slurm submission file
        user_command = "python run_stan.py"

        likelihood_variance_name = str(likelihood_variance_args)
        if type(eci_variance_args) == type(tuple([1])):
            eci_name = eci_variance_args[1]
        else:
            eci_name = str(eci_variance_args)
        dj.format_slurm_job(
            jobname="eci_var_"
            + str(eci_name)
            + "likelihood_"
            + likelihood_variance_name
            + "_crossval_"
            + str(count),
            hours=20,
            user_command=user_command,
            output_dir=this_run_path,
        )
        if submit_with_slurm:
            dj.submit_slurm_job(this_run_path)
        count += 1


def format_stan_model(
    eci_variance_args,
    likelihood_variance_args,
    eci_prior="normal",
    eci_variance_prior="gamma",
    likelihood_variance_prior="gamma",
    fixed_variance=False,
) -> str:
    """
    Parameters
    ----------
    eci_variance_args: tuple
        if fixed_variance is True, this should be a single float for eci variance. eg. eci_variance_args = 0.0016
        if fixed_variance is False, give arguments for the distribution as a tuple. eg. eci_variance_args = (1,1)
    likelihood_variance_args: tuple
        if fixed_variance is True, this should be a single float for the model variance. eg. likelihood_variance_args = 0.005
        if fixed_variance is False, give arguments for the distribution as a tuple. eg. eci_variance_args = (1,1)
    eci_prior: string
        Distribution type for ECI priors
    eci_variance_prior: string
        Distribution type for ECI variance prior
    likelihood_variance_prior: string
        Distribution type for model variance prior
    fixed_variance: Bool
        If True, model and ECI variance are fixed values. If false, they follow a distribution governed by hyperparameters.
        This choice will affect the eci and likelihood variance arg inputs; please read documentation for both.

    Returns
    -------
    model_template : str
        Formatted stan model template
    """
    warn(
        'This functinon "format_stan_model() is deprecated. Use "stan_model_formatter()" instead.',
        DeprecationWarning,
    )

    # Old args:
    # TODO: Add filter on string arguments

    supported_eci_priors = ["normal"]
    supported_eci_variance_priors = ["gamma"]
    supported_model_variance_priors = ["gamma"]

    assert eci_prior in supported_eci_priors, "Specified ECI prior is not suported."
    assert (
        eci_variance_prior in supported_eci_variance_priors
    ), "Specified ECI variance prior is not supported."

    assert (
        likelihood_variance_prior in supported_model_variance_priors
    ), "Specified model variance prior is not supported."
    # TODO: make this a single string that can be modified to allow any combination of fixed / non-fixed ECI and model variance priors.

    if fixed_variance:
        # If model and ECI variance are fixed to scalar values
        formatted_sigma = str(likelihood_variance_args)
        formatted_eci_variance = str(eci_variance_args)

        ce_model = Template(
            """data {
        int K; 
        int n_configs;
        matrix[n_configs, K] corr;
        vector[n_configs] energies;
    }
parameters {
        vector[K] eci;
    }
model 
    {
        real sigma = $formatted_sigma;
        for (k in 1:K){
            eci[k] ~ normal(0,$formatted_eci_variance);
        }
        energies ~ normal(corr * eci, sigma);
    }"""
        )
    else:
        # If model and ECI variance are not fixed (follows a distribution)
        formatted_sigma = str(likelihood_variance_args)
        formatted_eci_variance = eci_variance_prior + str(eci_variance_args)
        ce_model = Template(
            """data {
        int K; 
        int n_configs;
        matrix[n_configs, K] corr;
        vector[n_configs] energies;
    }
parameters {
        vector[K] eci;
        vector<lower=0>[K] eci_variance;
    }
model 
    {
        real sigma = $formatted_sigma;
        for (k in 1:K){
            eci_variance[k] ~ $formatted_eci_variance ;
            eci[k] ~ normal(0,eci_variance[k]);
        }
        energies ~ normal(corr * eci, sigma);
    }"""
        )
    model_template = ce_model.substitute(
        formatted_sigma=formatted_sigma, formatted_eci_variance=formatted_eci_variance,
    )
    # model_template = ce_model.substitute(formatted_eci_variance=formatted_eci_variance)
    return model_template


def plot_clex_hull_data_1_x(
    fit_dir,
    hall_of_fame_index,
    full_formation_energy_file="full_formation_energies.txt",
    custom_title="False",
):
    """plot_clex_hull_data_1_x(fit_dir, hall_of_fame_index, full_formation_energy_file='full_formation_energies.txt')

    Function to plot DFT energies, cluster expansion energies, DFT convex hull and cluster expansion convex hull.

    Args:
        fit_dir (str): absolute path to a casm cluster expansion fit.
        hall_of_fame_index (int or str): Integer index. "hall of fame" index for a specific fit (corresponding to a set of "Effective Cluster Interactions" or ECI).
        full_formation_energy_file (str): filename that contains the formation energy of all configurations of interest. Generated using a casm command

    Returns:
        fig: a python figure object.
    """
    # TODO: Definitely want to re-implement this with json input
    # Pre-define values to pull from data files
    # title is intended to be in the form of "casm_root_name_name_of_specific_fit_directory".
    if custom_title:
        title = custom_title
    else:
        title = fit_dir.split("/")[-3] + "_" + fit_dir.split("/")[-1]
    dft_scel_names = []
    clex_scel_names = []
    dft_hull_data = []
    clex_hull_data = []
    cv = None
    rms = None
    wrms = None
    below_hull_exists = False
    hall_of_fame_index = str(hall_of_fame_index)

    # Read necessary files
    os.chdir(fit_dir)
    files = glob("*")
    for f in files:
        if "_%s_dft_gs" % hall_of_fame_index in f:
            dft_hull_path = os.path.join(fit_dir, f)
            dft_hull_data = np.genfromtxt(
                dft_hull_path, skip_header=1, usecols=list(range(1, 10))
            ).astype(float)
            with open(dft_hull_path, "r") as dft_dat_file:
                dft_scel_names = [
                    row[0] for row in csv.reader(dft_dat_file, delimiter=" ")
                ]
                dft_scel_names = dft_scel_names[1:]

        if "_%s_clex_gs" % hall_of_fame_index in f:
            clex_hull_path = os.path.join(fit_dir, f)
            clex_hull_data = np.genfromtxt(
                clex_hull_path, skip_header=1, usecols=list(range(1, 10))
            ).astype(float)
            with open(clex_hull_path, "r") as clex_dat_file:
                clex_scel_names = [
                    row[0] for row in csv.reader(clex_dat_file, delimiter=" ")
                ]
                clex_scel_names = clex_scel_names[1:]

        if "_%s_below_hull" % hall_of_fame_index in f:
            below_hull_exists = True
            below_hull_path = os.path.join(fit_dir, f)
            below_hull_data = np.reshape(
                np.genfromtxt(
                    below_hull_path, skip_header=1, usecols=list(range(1, 10))
                ).astype(float),
                ((-1, 9)),
            )
            with open(below_hull_path, "r") as below_hull_file:
                below_hull_scel_names = [
                    row[0] for row in csv.reader(below_hull_file, delimiter=" ")
                ]
                below_hull_scel_names = below_hull_scel_names[1:]

        if "check.%s" % hall_of_fame_index in f:
            checkfile_path = os.path.join(fit_dir, f)
            with open(checkfile_path, "r") as checkfile:
                linecount = 0
                cv_rms_wrms_info_line = int
                for line in checkfile.readlines():
                    if (
                        line.strip() == "-- Check: individual 0  --"
                    ):  # % hall_of_fame_index:
                        cv_rms_wrms_info_line = linecount + 3

                    if linecount == cv_rms_wrms_info_line:
                        cv = float(line.split()[3])
                        rms = float(line.split()[4])
                        wrms = float(line.split()[5])
                    linecount += 1

    # Generate the plot
    fig = plt.figure()
    ax = fig.add_subplot()
    ax.text(
        0.80,
        0.90 * min(dft_hull_data[:, 5]),
        "CV:      %.10f\nRMS:    %.10f\nWRMS: %.10f" % (cv, rms, wrms),
        fontsize=20,
    )
    labels = []
    if custom_title:
        plt.title(custom_title, fontsize=30)
    else:
        plt.title(title, fontsize=30)
    plt.xlabel(r"Composition", fontsize=22)
    plt.ylabel(r"Energy $\frac{eV}{prim}$", fontsize=22)
    plt.xticks(fontsize=22)
    plt.yticks(fontsize=22)
    plt.plot(dft_hull_data[:, 1], dft_hull_data[:, 5], marker="o", color="xkcd:crimson")
    labels.append("DFT Hull")
    plt.plot(
        clex_hull_data[:, 1],
        clex_hull_data[:, 8],
        marker="o",
        linestyle="dashed",
        color="b",
    )
    labels.append("ClEx Hull")
    plt.scatter(dft_hull_data[:, 1], dft_hull_data[:, 8], color="k")
    labels.append("Clex Prediction of DFT Hull")

    if full_formation_energy_file:
        # format:
        # run casm query -k comp formation_energy hull_dist clex clex_hull_dist -o full_formation_energies.txt
        #            configname    selected           comp(a)    formation_energy    hull_dist(MASTER,atom_frac)        clex()    clex_hull_dist(MASTER,atom_frac)
        datafile = full_formation_energy_file
        data = np.genfromtxt(datafile, skip_header=1, usecols=list(range(2, 7))).astype(
            float
        )
        composition = data[:, 0]
        dft_formation_energy = data[:, 1]
        clex_formation_energy = data[:, 3]
        plt.scatter(composition, dft_formation_energy, color="salmon")
        labels.append("DFT energies")
        plt.scatter(composition, clex_formation_energy, marker="x", color="skyblue")
        labels.append("ClEx energies")

    # TODO: This implementation is wrong. This is the distance below the hull (energy difference) not the actual energy.
    if below_hull_exists:
        plt.scatter(below_hull_data[:, 1], below_hull_data[:, 7], marker="+", color="k")
        labels.append("Clex Below Clex Prediction of DFT Hull Configs")
    else:
        print("'_%s_below_hull' file doesn't exist" % hall_of_fame_index)

    plt.legend(labels, loc="lower left", fontsize=20)

    fig = plt.gcf()
    return fig


def simplex_corner_weights(
    interior_point: np.ndarray, corner_points: np.ndarray
) -> np.ndarray:
    """Calculates the linear combination of simplex corners required to produce a point within the simplex.

    Parameters:
    -----------
    interior_point: numpy.ndarray
        Composition row vector of a point within a hull simplex.
    corner_points: numpy.ndarray
        Matrix of composition row vectors of all corner points of a hull simplex.

    Returns:
    --------
    weights: numpy.ndarray
        Vector of weights for each corner point. Matrix multiplying wieghts @ corner_corr_matrix will give the linear combination of simplex correlation vectors which,
        when multiplied with ECI, gives the hull distance of the correlation represented by interior_point.
    """
    # Add a 1 to the end of interior_point and a column of ones to simplex_corners to enforce that the sum of weights is 1.
    interior_point = np.array(interior_point).reshape(1, -1)
    simplex_corners = np.hstack((corner_points, np.ones((simplex_corners.shape[0], 1))))

    # Calculate the weights for each simplex corner point.
    weights = interior_point @ np.linalg.pinv(simplex_corners)

    return weights
