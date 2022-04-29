from typing import Any, Dict, List, Optional, Tuple, Union

import xarray as xr
from prefect import Flow, Parameter, task
from xpersist import CacheStore
from xpersist.prefect.result import XpersistResult

from cmip6_downscaling import config, runtimes
from cmip6_downscaling.methods.detrend import calc_epoch_trend, remove_epoch_trend
from cmip6_downscaling.methods.maca import maca_bias_correction, maca_construct_analogs
from cmip6_downscaling.methods.regions import combine_outputs, generate_subdomains

# TODO: figure out where these are located
from cmip6_downscaling.tasks.common_tasks import (
    get_coarse_obs_task,
    get_gcm_task,
    get_obs_task,
    path_builder_task,
    rechunker_task,
)
from cmip6_downscaling.workflows.paths import (
    make_bias_corrected_gcm_path,
    make_epoch_adjusted_downscaled_gcm_path,
    make_epoch_adjusted_gcm_path,
    make_epoch_replaced_downscaled_gcm_path,
    make_epoch_trend_path,
    make_maca_output_path,
)
from cmip6_downscaling.workflows.utils import rechunk_zarr_array_with_caching, regrid_ds

from ..methods.detrend import calc_epoch_trend, remove_epoch_trend
from ..methods.maca import maca_bias_correction_task, maca_construct_analogs
from ..methods.regions import combine_outputs, generate_subdomains
from . import config, runtimes

runtime = runtimes.get_runtime()


intermediate_cache_store = CacheStore(
    config.get('storage.intermediate.uri'),
    storage_options=config.get('storage.intermediate.storage_options'),
)
results_cache_store = CacheStore(
    config.get('storage.results.uri'), storage_options=config.get('storage.results.storage_options')
)
serializer_dump_kwargs = config.get('storage.xpersist_overwrite')

# TODO: make it so we can return two target paths cached
@task(log_stdout=True, nout=2)
def calc_epoch_trend_task(
    gcm_path,
    run_parameters,
    **kwargs,
):
    """
    Task to calculate the epoch trends in MACA. The epoch trend is a long term rolling average, and thus the first and
    last few years of the output suffers from edge effects. Thus, this task gets additional years for calculating the
    rolling averages.

    Parameters
    ----------
    gcm_path
    run_parameters

    Returns
    -------
    trend: xr.Dataset
        The long term average trend
    """

    ds_hash = str_to_hash(str(gcm_path)+run_parameters.run_id_hash)
    target = intermediate_dir / 'epoch_trend' / ds_hash

    print(target)
    if use_cache and zmetadata_exists(target):
        print(f'found existing target: {target}')
        return target

    # obtain a buffer period for both training and prediction period equal to half of the year_rolling_window
    y_offset = int((run_parameters.year_rolling_window - 1) / 2)

    train_start = int(run_parameters.train_period_start) - run_parameters.y_offset
    train_end = int(run_parameters.train_period_end) + run_parameters.y_offset
    predict_start = int(run_parameters.predict_period_start) - run_parameters.y_offset
    predict_end = int(run_parameters.predict_period_end) + run_parameters.y_offset
    # TODO: why do we do this step?
    # make sure there are no overlapping years
    if train_end > int(run_parameters.predict_period_start):
        train_end = int(run_parameters.predict_period_start) - 1
        predict_start = int(run_parameters.predict_period_start)
    elif train_end > predict_start:
        predict_start = train_end + 1

    ds_gcm_full_time = xr.open_zarr(gcm_path)

    # note that this is the non-buffered slice
    historical_period = slice(run_parameters.train_period_start, run_parameters.train_period_end)
    predict_period = slice(run_parameters.predict_period_start, run_parameters.predict_period_end)
    trend = calc_epoch_trend(
        data=ds_gcm_full_time,
        historical_period=historical_period,
        day_rolling_window=run_parameters.day_rolling_window,
        year_rolling_window=run_parameters.year_rolling_window,
    )

    hist_trend = trend.sel(time=historical_period)
    pred_trend = trend.sel(time=predict_period)

    trend = xr.combine_by_coords([hist_trend, pred_trend], combine_attrs='drop_conflicts')
    # TODO: write out dataset
    trend.attrs.update({'title': title}, **get_cf_global_attrs(version=version))

    trend.to_zarr(target, mode='w')
    detrended_data = ds_gcm_full_time - trend
    # TODO: save detrended_data to a different target

    # blocking_to_zarr(subset, target)
    return target_trend, target_data


remove_epoch_trend_task = task(
    remove_epoch_trend,
    result=XpersistResult(
        intermediate_cache_store,
        serializer="xarray.zarr",
        serializer_dump_kwargs=serializer_dump_kwargs,
    ),
    target=make_epoch_adjusted_gcm_path,
)


@task(nout=4)
def get_subdomains_task(
    ds_obs: xr.Dataset, buffer_size: Union[float, int] = 5, region_def: str = 'ar6'
):
    """
    Get the definition of subdomains according to region_def specified.

    Parameters
    ----------
    ds_obs: xr.Dataset
        Observation dataset
    buffer_size : int or float
        Buffer size in unit of degree. for each subdomain, how much extra area to run for each subdomain
    region_def : str
        Subregion definition name. Options are `'ar6'` or `'srex'`. See the docs https://regionmask.readthedocs.io/en/stable/defined_scientific.html for more details.

    Returns
    -------
    subdomains_list: List
        List of all subdomain boundaries sorted by the region code
    subdomains_dict : dict
        Dictionary mapping subdomain code to bounding boxes ([min_lon, min_lat, max_lon, max_lat]) for each subdomain
    mask : xarray.DataArray
        Mask of which subdomain code to use for each grid cell
    n_subdomains: int
        The number of subdomains that are included
    """
    subdomains_dict, mask = generate_subdomains(
        ex_output_grid=ds_obs.isel(time=0),
        buffer_size=buffer_size,
        region_def=region_def,
    )

    subdomains_list = [subdomains_dict[k] for k in sorted(subdomains_dict.keys())]
    return subdomains_list, subdomains_dict, mask, len(subdomains_list)


@task(nout=3)
def subset_task(
    ds_gcm: xr.Dataset,
    ds_obs_coarse: xr.Dataset,
    ds_obs_fine: xr.Dataset,
    subdomains_list: List[Tuple[float, float, float, float]],
):
    """
    Subset each dataset spatially into areas within each subdomain bound.

    Parameters
    ----------
    ds_gcm: xr.Dataset
        GCM dataset, original/coarse resolution
    ds_obs_coarse: xr.Dataset
        Observation dataset coarsened to the GCM resolution
    ds_obs_fine: xr.Dataset
        Observation dataset, original/fine resolution
    subdomains_list: List
        List of all subdomain boundaries sorted by the region code

    Returns
    -------
    ds_gcm_list: List
        List of subsetted GCM datasets in the same order of subdomains_list
    ds_obs_coarse_list: List
        List of subsetted coarened obs datasets in the same order of subdomains_list
    ds_obs_fine_list: List
        List of subsetted fine obs datasets in the same order of subdomains_list
    """
    ds_gcm_list, ds_obs_coarse_list, ds_obs_fine_list = [], [], []
    for (min_lon, min_lat, max_lon, max_lat) in subdomains_list:
        lat_slice = slice(max_lat, min_lat)
        lon_slice = slice(min_lon, max_lon)
        ds_gcm_list.append(ds_gcm.sel(lat=lat_slice, lon=lon_slice))
        ds_obs_coarse_list.append(ds_obs_coarse.sel(lat=lat_slice, lon=lon_slice))
        ds_obs_fine_list.append(ds_obs_fine.sel(lat=lat_slice, lon=lon_slice))

    return ds_gcm_list, ds_obs_coarse_list, ds_obs_fine_list


maca_construct_analogs_task = task(
    maca_construct_analogs,
    result=XpersistResult(
        intermediate_cache_store,
        serializer="xarray.zarr",
        serializer_dump_kwargs=serializer_dump_kwargs,
    ),
    target=make_epoch_adjusted_downscaled_gcm_path,
)


@task(
    checkpoint=True,
    result=XpersistResult(
        intermediate_cache_store,
        serializer="xarray.zarr",
        serializer_dump_kwargs=serializer_dump_kwargs,
    ),
    target=make_epoch_adjusted_downscaled_gcm_path,
)
def combine_outputs_task(
    ds_list: List[xr.Dataset],
    subdomains_dict: Dict[Union[int, float], Any],
    mask: xr.DataArray,
    **kwargs,
):
    """
    Combine a list of datasets spatially according to the subdomain list and mask.

    Parameters
    ----------
    ds_list: List[xr.Dataset]
        List of datasets to be combined
    subdomains_dict : dict
        Dictionary mapping subdomain code to bounding boxes ([min_lon, min_lat, max_lon, max_lat]) for each subdomain
    mask : xarray.DataArray
        Mask of which subdomain code to use for each grid cell

    Returns
    -------
    combined_output: xr.Dataset
        The combined output
    """
    ds_dict = {k: ds_list.pop(0) for k in sorted(subdomains_dict.keys())}
    return combine_outputs(ds_dict=ds_dict, mask=mask)


@task(
    checkpoint=True,
    result=XpersistResult(
        intermediate_cache_store,
        serializer="xarray.zarr",
        serializer_dump_kwargs=serializer_dump_kwargs,
    ),
    target=make_epoch_replaced_downscaled_gcm_path,
)
def maca_epoch_replacement_task(
    ds_gcm_fine: xr.Dataset,
    trend_coarse: xr.Dataset,
    **kwargs,
) -> xr.Dataset:
    """
    Replace the epoch trend. The trend was calculated on coarse scale GCM, so the trend is first interpolated
    into the finer grid before being added back into the downscaled GCM.

    Parameters
    ----------
    ds_gcm_fine: xr.Dataset
        Downscaled GCM, fine/observation resolution
    trend_coarse: xr.Dataset
        The epoch trend, coarse/original GCM resolution

    Returns
    -------
    epoch_replaced_gcm: xr.Dataset
        The downscaled GCM dataset with the epoch trend replaced back
    """
    trend_fine = regrid_ds(
        ds=trend_coarse,
        target_grid_ds=ds_gcm_fine.isel(time=0).chunk({'lat': -1, 'lon': -1}),
    )

    return ds_gcm_fine + trend_fine


@task(
    checkpoint=True,
    result=XpersistResult(
        intermediate_cache_store,
        serializer="xarray.zarr",
        serializer_dump_kwargs=serializer_dump_kwargs,
    ),
    target=make_maca_output_path,
)
def maca_fine_bias_correction_task(
    ds_gcm: xr.Dataset,
    ds_obs: xr.Dataset,
    train_period_start: str,
    train_period_end: str,
    label: str,
    batch_size: Optional[int] = 15,
    buffer_size: Optional[int] = 15,
    **kwargs,
):
    """
    Task that implements the fine scale bias correction in MACA. The historical GCM is mapped to historical
    coarsened observation in the bias correction. Rechunks the GCM data to match observation data because
    the bias correction model in skdownscale requires these datasets to have the same chunks/blocks.

    ds_gcm: xr.Dataset
        GCM dataset
    ds_obs: xr.Dataset
        Observation dataset
    train_period_start: str
        Start year of training/historical period
    train_period_end: str
        End year of training/historical period
    variables: List[str]
        Names of the variables used in obs and gcm dataset (including features and label)
    chunking_approach: str
        'full_space', 'full_time', 'matched' or None
    batch_size: Optional[int]
        The batch size in terms of day of year to bias correct together
    buffer_size: Optional[int]
        The buffer size in terms of day of year to include in the bias correction

    Returns
    -------
    bias_corrected: xr.Dataset
        Bias corrected GCM dataset
    """
    ds_gcm_rechunked = rechunk_zarr_array_with_caching(
        zarr_array=ds_gcm, template_chunk_array=ds_obs
    )

    historical_period = slice(train_period_start, train_period_end)
    bias_corrected = maca_bias_correction(
        ds_gcm=ds_gcm_rechunked,
        ds_obs=ds_obs,
        historical_period=historical_period,
        variables=[label],
        batch_size=batch_size,
        buffer_size=buffer_size,
    )

    return bias_corrected


with Flow(
    name='maca',
    storage=runtime.storage,
    run_config=runtime.run_config,
    executor=runtime.executor,
) as maca_flow:
    # following https://climate.northwestknowledge.net/MACA/MACAmethod.php
    run_parameters = make_run_parameters()
    # obs = Parameter("OBS")
    # gcm = Parameter("GCM")
    # scenario = Parameter("SCENARIO")
    # label = Parameter("LABEL")

    # train_period_start = Parameter("TRAIN_PERIOD_START")
    # train_period_end = Parameter("TRAIN_PERIOD_END")
    # predict_period_start = Parameter("PREDICT_PERIOD_START")
    # predict_period_end = Parameter("PREDICT_PERIOD_END")

    # epoch_adjustment_day_rolling_window = Parameter("EPOCH_ADJUSTMENT_DAY_ROLLING_WINDOW")
    # epoch_adjustment_year_rolling_window = Parameter("EPOCH_ADJUSTMENT_YEAR_ROLLING_WINDOW")
    # bias_correction_batch_size = Parameter("BIAS_CORRECTION_BATCH_SIZE")
    # bias_correction_buffer_size = Parameter("BIAS_CORRECTION_BUFFER_SIZE")
    # constructed_analog_n_analogs = Parameter("CONSTRUCTED_ANALOG_N_ANALOGS")
    # constructed_analog_doy_range = Parameter("CONSTRUCTED_ANALOG_DOY_RANGE")

    ## Step 0: tasks to get inputs and set up
    ## Step 1: Common Grid -- this step is skipped since it seems like an unnecessary extra step for convenience

    # dictionary with information to build appropriate paths for caching

    # get original resolution observations
    p['obs_path'] = get_obs(run_parameters)

    p['obs_full_space_path'] = rechunk(path=p['obs_path'], pattern='full_space')
    
    p['experiment_train_path'] = get_experiment(run_parameters, time_subset='train_period')

    # get coarsened resolution observations
    # this coarse obs is going to be used in bias correction next, so rechunk into full time first
    p['coarse_obs_path'] = regrid(
        p['obs_full_space_path'], p['experiment_train_path'], weights_path=p['obs_to_gcm_weights']
    )

    ## Step 2: Epoch Adjustment -- all variables undergo this epoch adjustment
    # TODO: in order to properly do a 31 year average, might need to run this step with the entire future period in GCMs
    # but this might be too memory intensive in the later task
    # BIG JOB
    p['coarse_epoch_trend_path'], p['detrended_data_path'] = calc_epoch_trend_task(p['experiment_train_path'], run_parameters)

    # get gcm
    # 1981-2100 extent time subset
    p['experiment_predict_path'] = get_experiment(run_parameters, time_subset='predict_period')

    p['experiment_predict_full_time_path'] = rechunk(p['experiment_predict_path'], pattern='full_time')

    ## Step 3: Coarse Bias Correction
    # rechunk to make detrended data match the coarse obs
    # TODO: check whether we want full-time AND template (or just template)
    p['detrend_gcm_rechunk_coarse_obs'] = rechunk(p['experiment_predict_path'], template=p['coarse_obs_path'])

    maca_coarse_bias_correction_task(ds_gcm = p['detrended_data_path'],
                ds_obs=p['coarse_obs_path'], # to-do FULL TIME?
                run_parameters=run_parameters.
                )
    # do epoch adjustment again for multiplicative variables, see MACA v1 vs. v2 guide for details
    # if label in ['pr', 'huss', 'vas', 'uas']:
    #     coarse_epoch_trend_2 = calc_epoch_trend_task(
    #         data=bias_corrected_gcm,
    #         train_period_start=train_period_start,
    #         train_period_end=train_period_end,
    #         day_rolling_window=epoch_adjustment_day_rolling_window,
    #         year_rolling_window=epoch_adjustment_year_rolling_window,
    #         gcm_identifier=f'{gcm_identifier}_2',
    #     )

    #     bias_corrected_gcm = remove_epoch_trend_task(
    #         data=bias_corrected_gcm,
    #         trend=coarse_epoch_trend_2,
    #         day_rolling_window=epoch_adjustment_day_rolling_window,
    #         year_rolling_window=epoch_adjustment_year_rolling_window,
    #         gcm_identifier=f'{gcm_identifier}_2',
    #     )

    ## Step 4: Constructed Analogs
    # rechunk into full space and cache the output
    p['bc_gcm_full_space'] = rechunk(p['bc_gcm'], pattern='full_space')

    # subset into regions
    # subdomains_list, subdomains_dict, mask, n_subdomains = get_subdomains_task(
    #     ds_obs=ds_obs_full_space
    # )

    # everything should be rechunked to full space and then subset
    p['coarse_obs_path'] = regrid(
        p['obs_full_space_path'], p['experiment_train_path'], weights_path=p['obs_to_gcm_weights']
    )

    # all inputs into the map function needs to be a list
    # ds_gcm_list, ds_obs_coarse_list, ds_obs_fine_list = subset_task(
    #     ds_gcm=bias_corrected_gcm_full_space,
    #     ds_obs_coarse=ds_obs_coarse_full_space,
    #     ds_obs_fine=ds_obs_full_space,
    #     subdomains_list=subdomains_list,
    # )

    # downscaling by constructing analogs
    # downscaled_gcm_list = maca_construct_analogs_task.map(
    #     ds_gcm=ds_gcm_list,
    #     ds_obs_coarse=ds_obs_coarse_list,
    #     ds_obs_fine=ds_obs_fine_list,
    #     subdomain_bound=subdomains_list,
    #     n_analogs=[constructed_analog_n_analogs] * n_subdomains,
    #     doy_range=[constructed_analog_doy_range] * n_subdomains,
    #     gcm_identifier=[gcm_identifier] * n_subdomains,
    #     label=[label] * n_subdomains,
    # )

    # combine back into full domain
    # combined_downscaled_output = combine_outputs_task(
    #     ds_list=downscaled_gcm_list,
    #     subdomains_dict=subdomains_dict,
    #     mask=mask,
    #     gcm_identifier=gcm_identifier,
    #     label=label,
    # )

    # ## Step 5: Epoch Replacement
    # if label in ['pr', 'huss', 'vas', 'uas']:
    #     combined_downscaled_output = maca_epoch_replacement_task(
    #         ds_gcm_fine=combined_downscaled_output,
    #         trend_coarse=coarse_epoch_trend_2,
    #         day_rolling_window=epoch_adjustment_day_rolling_window,
    #         year_rolling_window=epoch_adjustment_year_rolling_window,
    #         gcm_identifier=f'{gcm_identifier}_2',
    #     )

    # epoch_replaced_gcm = maca_epoch_replacement_task(
    #     ds_gcm_fine=combined_downscaled_output,
    #     trend_coarse=coarse_epoch_trend,
    #     day_rolling_window=epoch_adjustment_day_rolling_window,
    #     year_rolling_window=epoch_adjustment_year_rolling_window,
    #     gcm_identifier=gcm_identifier,
    # )
    p['obs_full_time_path'] = rechunk(path=p['obs_path'], pattern="full_time")

    ## Step 6: Fine Bias Correction

    # final_output = maca_fine_bias_correction_task(
    #     ds_gcm=epoch_replaced_gcm,
    #     ds_obs=ds_obs_full_time,
    #     train_period_start=train_period_start,
    #     train_period_end=train_period_end,
    #     variables=[label],
    #     batch_size=bias_correction_batch_size,
    #     buffer_size=bias_correction_buffer_size,
    #     gcm_identifier=gcm_identifier,
    # )
