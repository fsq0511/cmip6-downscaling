import os

os.environ["PREFECT__FLOWS__CHECKPOINTING"] = "True"

import fsspec
import intake
import pandas as pd
import xarray as xr
import xesmf as xe
import zarr
from prefect import Flow, Parameter, task
from skdownscale.pointwise_models import BcAbsolute, PointWiseDownscaler
from cmip6_downscaling.workflows.utils import rechunk_dataset


# https://rechunker.readthedocs.io/en/latest/

# Rechunker Example code:
# import xarray as xr
# xr.set_options(display_style='text')
# import zarr
# import dask.array as dsa
# import fsspec
# from rechunker import rechunk
# import os


# path = 'az://cmip6/ERA5/1979/01/air_pressure_at_mean_sea_level.zarr'
# coarse_obs_store = fsspec.get_mapper(path, connection_string=connection_string)
# ds = xr.open_zarr(coarse_obs_store, consolidated=True) # load the obs that fits the grid of the experiment
# target_chunks_dict = {'time': 1, 'lat': 721, 'lon': 1440}
# # target_chunks = (1,721,1440)
# max_mem = '1GB'


# store_tmp = fsspec.get_mapper('az://cmip6/rechunker_temp5.zarr', connection_string=connection_string)
# store_tgt = fsspec.get_mapper('az://cmip6/rechunker_target5.zarr', connection_string=connection_string)

# r = rechunk(ds, target_chunks_dict, max_mem,
#                       store_tgt, temp_store=store_tmp)
# result = r.execute()


# store_tgt = fsspec.get_mapper('az://cmip6/rechunker_target2.zarr', connection_string=connection_string)

# xdf = xr.open_zarr( store_tgt)


# specify your run parameters by reading in a text config file?
# potential test: ensure that the run_id in the config file is
# the same as the name of the config file? to make sure that
# you've created a new config file for a new run?
chunks = {"lat": 10, "lon": 10, "time": -1}
connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")


def convert_to_360(lon):
    if lon > 0:
        return lon
    elif lon < 0:
        return 360 + lon


test_specs = {
    "domain": {
        "lat": slice(50, 45),
        "lon": slice(convert_to_360(-124.8), convert_to_360(-120.0)),
    }
}  # bounding box of downscaling region

connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
account_key = os.environ.get("account_key")
run_hyperparameters = {
    "FLOW_NAME": "BCSD_testing",  # want to populate with unique string name?
    # "MODEL": "BCSD",
    # "VARIABLES": ["tasmax"],
    # "INTERPOLATION": "bilinear",
    "GCMS": ["MIROC6"],
    # "EXPERIMENT_ID": 'CMIP.MIROC.MIROC6.historical.day.gn',
    # "SCENARIOS": ["ssp370"],
    "TRAIN_PERIOD_START": "1990",
    "TRAIN_PERIOD_END": "1992",
    "PREDICT_PERIOD_START": "2080",
    "PREDICT_PERIOD_END": "2082",
    # "DOMAIN": {'lat': slice(50, 45),
    #             'lon': slice(235.2, 240.0)},
    "VARIABLE": "tasmax",
    # "GRID_NAME": None,
    # "INTERMEDIATE_STORE": "scratch/cmip6",
    # "FINAL_STORE": "cmip6/downscaled",
    # "SAVE_MODEL": False,
    "OBS": "ERA5",
}
flow_name = run_hyperparameters.pop("FLOW_NAME")  # pop it out because if you leave it in the dict
# but don't call it as a parameter it'll complain

# converts cmip standard names to ERA5 names
# can be deleted once new ERA5 dataset complete
variable_name_dict = {
    "tasmax": "air_temperature_at_2_metres_1hour_Maximum",
    "tasmin": "air_temperature_at_2_metres_1hour_Minimum",
    "pr": "precipitation_amount_1hour_Accumulation",
}
chunks = {"lat": 10, "lon": 10, "time": -1}


def load_cmip_dictionary(
    activity_ids=["CMIP", "ScenarioMIP"],
    experiment_ids=["historical", "ssp370"],  # , "ssp126", "ssp245",  "ssp585"
    member_ids=["r1i1p1f1"],
    source_ids=["MIROC6"],  # BCC-CSM2-MR"]
    table_ids=["day"],
    grid_labels=["gn"],
    variable_ids=["tasmax"],
):
    """Loads CMIP6 GCM dataset dictionary based on input criteria.

    Parameters
    ----------
    activity_ids : list, optional
        [activity_ids in CMIP6 catalog], by default ["CMIP", "ScenarioMIP"],
    experiment_ids : list, optional
        [experiment_ids in CMIP6 catalog], by default ["historical", "ssp370"],  ex:#  "ssp126", "ssp245",  "ssp585"
    member_ids : list, optional
        [member_ids in CMIP6 catalog], by default ["r1i1p1f1"]
    source_ids : list, optional
        [source_ids in CMIP6 catalog], by default ["MIROC6"]
    table_ids : list, optional
        [table_ids in CMIP6 catalog], by default ["day"]
    grid_labels : list, optional
        [grid_labels in CMIP6 catalog], by default ["gn"]
    variable_ids : list, optional
        [variable_ids in CMIP6 catalog], by default ['tasmax']

    Returns
    -------
    [dictionary]
        [dictionary containing available xarray datasets]
    """
    col_url = "https://cmip6downscaling.blob.core.windows.net/cmip6/pangeo-cmip6.json"
    print('intake')
    full_subset = intake.open_esm_datastore(col_url).search(
        activity_id=activity_ids,
        experiment_id=experiment_ids,
        member_id=member_ids,
        source_id=source_ids,
        table_id=table_ids,
        grid_label=grid_labels,
        variable_id=variable_ids,
    )
    print('to dictionary')
    ds_dict = full_subset.to_dataset_dict(
        zarr_kwargs={"consolidated": True, "decode_times": True, "use_cftime": True},
        storage_options={
            "account_name": "cmip6downscaling",
            "account_key": os.environ.get("AccountKey", None),
        },
        progressbar=False,
    )
    print('return dict')
    return ds_dict


# # tests


def get_store(bucket, prefix, account_key=None):
    """helper function to create a zarr store"""

    if account_key is None:
        account_key = os.environ.get("AccountKey", None)

    store = zarr.storage.ABSStore(
        bucket, prefix=prefix, account_name="cmip6downscaling", account_key=account_key
    )
    return store


def open_era5(var):
    all_era5_stores = pd.read_csv("/Users/orianachegwidden/Desktop/ERA5_catalog.csv")["ERA5"].values
    era5_stores = [
        store.split("az://cmip6/")[1]
        for store in all_era5_stores
        if variable_name_dict[var] in store
    ]
    print('getting stores')
    store_list = [get_store(bucket="cmip6", prefix=prefix) for prefix in era5_stores]
    print('open mfdataset')
    ds = xr.open_mfdataset(store_list, engine="zarr", concat_dim="time").drop("time1_bounds")
    print('return mfdataset')
    return ds


def load_obs(obs_id, variable, time_period, domain):
    """

    Parameters
    ----------
    obs_id : [type]
        [description]
    variable : [type]
        [description]
    time_period : [type]
        [description]
    domain : [type]
        [description]

    Returns
    -------
    [xarray dataset]
        [Chunked {time:-1,lat=10,lon=10}]
    """
    ## most of this can be deleted once new ERA5 dataset complete
    if obs_id == "ERA5":
        print('open era5')
        full_obs = open_era5(variable)
        print('resample era5')
        obs = (
            full_obs[variable_name_dict[variable]]
            .sel(time=time_period, lon=domain["lon"], lat=domain["lat"])
            .resample(time="1d")
            .mean()
            .rename(variable)
            # .load(scheduler="threads")  # GOAL! REMOVE THE `LOAD`!
        )
        obs = obs.resample(time="1D").max()
    return obs


###### ALL OF THIS IS ICING ON THE CAKE- NOT NECESSARY NOW
# @task
# def setuprun()
#     '''
#     based upon input gcms/scenarios determine which tasks you need to complete
#     then the remaining tasks will loop through (anticipates a not-dense set of gcms/scenarios/dsms)
#     - shared across downscaling methods
#     '''

#     return experiment_ids

# def get_grid(dataset):
#     if dataset=='ERA5':
#         return '25km'
#     elif dataset=='CMIP.MIROC.MIROC6.historical.day.gn':
#         return 'not25km'

# def check_preparation(experiment):
#     # what grid are you on
#     # does the weights file for that grid exist
#     # does the coarse obs for that grid exist
#     # do the spatial anomolies for those coarse obs (time period matters) exist


@task(checkpoint=True)
def get_weight_file(grid_name_gcm, grid_name_obs):
    happy = "yeah"
    return happy


# @task(checkpoint=True)
def get_coarse_obs(obs, gcm_ds_single_time_slice):
    """[summary]

    Parameters
    ----------
    obs : xarray dataset
        chunked in space (lat=-1, lon=-1, time=1)
    gcm_ds_single_time_slice : [type]
        Chunked in space

    Returns
    -------
    [type]
        [Chunked in time]
    """
    # TEST TODO: Check that obs is chunked appropriately and throw error if not
    # Like: assert obs.chunks == (lat=-1, lon=-1, time=1) - then eventually we can move the rechunker in as an `else`
    regridder = xe.Regridder(obs, gcm_ds_single_time_slice, "bilinear")

    obs_coarse = regridder(obs)
    # then write it out
    # # obs_coarse.to_zarr()
    return obs_coarse


def get_spatial_anomolies(coarse_obs, fine_obs):
    # check if this has been done, if do the math
    # if it has been done, just read them in
    """[summary]

    Parameters
    ----------
    coarse_obs : [type]
        [chunked in space (lat=-1,lon=-1,time=1)]
    fine_obs : [type]
        [chunked in space (lat=-1,lon=-1,time=1)]

    Returns
    -------
    [type]
        [description]
    """
    # check chunks specs & run regridder if needed
    regridder = xe.Regridder(
        coarse_obs, fine_obs.isel(time=0), "bilinear", extrap_method="nearest_s2d"
    )

    obs_interpolated = regridder(coarse_obs)
    spatial_anomolies = obs_interpolated - fine_obs
    seasonal_cycle_spatial_anomolies = spatial_anomolies.groupby("time.month").mean()

    return seasonal_cycle_spatial_anomolies


@task(log_stdout=True)
def print_x(x):
    print(x)


@task(  # target="{flow_name}.txt", checkpoint=True,
    # result=LocalResult(dir="~/.prefect"),
    # cache_for=datetime.timedelta(hours=1),
    log_stdout=True
)
def preprocess_bcsd(
    gcm,
    obs_id,
    train_period_start,
    train_period_end,
    variable,
    out_bucket,
    domain,
    rerun=True,
):

    """
    take experiment id and return the gcm, obs at the coarse scale to match that
    gcm's grid, and the spatial anomolies, all chunked in a performant way
    create checkpoint when this has run
    TODO: do we want to assign this to a class? write out? probably
    """
    # check which components you'll need to create in order to
    # run the flow for this experiment
    # inputs_complete = check_preparation(experiment)
    # TODO: this part (working with grid and getting weight file) will just help us not repeat
    # the regridding step by grabbing the grid and checking for an existing weights file, but
    # for now we'll just not try to do that checking and we'll just regrid regardless (optimize later)
    ## grid_name_gcm = get_grid(gcm)
    ## grid_name_obs = get_grid(obs)
    ## get_weight_file_task = task(get_weight_file,
    ##             checkpoint=True,
    ##             result=AzureResult('cmip6'),
    ##             target='grid.zarr')
    ## weight_file = get_weight_file_task(grid_name_gcm, grid_name_obs)
    coarse_obs_store = fsspec.get_mapper(
        f"az://cmip6/intermediates/{obs_id}_{gcm[0]}_{train_period_start}_{train_period_end}_{variable}.zarr",
        connection_string=connection_string,
    )
    spatial_anomolies_store = fsspec.get_mapper(
        f"az://cmip6/intermediates/anomalies_{obs_id}_{gcm[0]}_{train_period_start}_{train_period_end}_{variable}.zarr",
        connection_string=connection_string,
    )
    if rerun:
        print('load obs')
        obs = load_obs(
            obs_id,
            variable,
            time_period=slice(train_period_start, train_period_end),
            domain=domain,
        )  # We want it chunked in space. (time=1,lat=-1, lon=-1)
        print('load cmip')
        gcm_ds = load_cmip_dictionary(source_ids=gcm, variable_ids=[variable])[
            "CMIP.MIROC.MIROC6.historical.day.gn"
        ]  # This comes chunked in space (time~600,lat-1,lon-1), which is good.

        # Check whether gcm latitudes go from low to high, if so, swap them to match ERA5 which goes from high to low
        # after we create era5 daily processed product we should still leave this in but should switch < to > in if statement
        if gcm_ds.lat[0] < gcm_ds.lat[-1]:
            gcm_ds = gcm_ds.reindex({"lat": gcm_ds.lat[::-1]})
        gcm_ds_single_time_slice = gcm_ds.sel(domain).isel(time=0).load() #TODO: check whether we need the load here
        rechunked_obs, rechunked_obs_path = rechunk_dataset(obs, 
                                                            chunks_dict={'tasmax': (1, -1, -1)}, 
                                                            connection_string=connection_string,
                                                            max_mem='1GB')

        # we want to pass rechunked obs to both get_coarse_obs and get_spatial_anomalies
        # since they both work in map space instead of time space
        coarse_obs = get_coarse_obs(
            rechunked_obs,
            gcm_ds_single_time_slice,
        )
        spatial_anomolies = get_spatial_anomolies(coarse_obs, rechunked_obs)

        # save the coarse obs because it might be used by another gcm
        coarse_obs.to_dataset(name=variable).to_zarr(coarse_obs_store, mode="w", consolidated=True)

        spatial_anomolies.to_dataset(name=variable).to_zarr(
            spatial_anomolies_store, mode="w", consolidated=True)
        return coarse_obs


# @task
# def biascorrect(X, y, train_period, predict_period, model):
#     '''
#     fit the model at coarse gcm scale and then predict!
# fit
# predict
# return y_hat
#     '''

# @task
# def postprocess(y_hat, spatial_anomolies):
#     '''
#     Interpolate, add back in the spatial anomolies from the coarsening, and write to store
#     '''
#     y_hat.
# write out downscaled bias-corrected
def gcm_munge(ds):
    if ds.lat[0] < ds.lat[-1]:
        ds = ds.reindex({"lat": ds.lat[::-1]})
    ds = ds.drop(["lat_bnds", "lon_bnds", "time_bnds", "height", "member_id"]).squeeze(drop=True)
    return ds


@task(log_stdout=True, nout=3)
def prep_bcsd_inputs(
    # coarse_obs,
    gcm,
    obs_id,
    train_period_start,
    train_period_end,
    predict_period_start,
    predict_period_end,
    variable,
    out_bucket,
    domain,
):
    """[summary]

    Parameters
    ----------
    gcm : [type]
        [description]
    obs_id : [type]
        [description]
    train_period_start : [type]
        [description]
    train_period_end : [type]
        [description]
    predict_period_start : [type]
        [description]
    predict_period_end : [type]
        [description]
    variable : [type]
        [description]
    out_bucket : [type]
        [description]
    domain : [type]
        [description]

    Returns
    -------
    xarray datasets
        Chunked in lat=10,lon=10,time=-1
    """
    # X = load_cmip_dictionary()["CMIP.MIROC.MIROC6.historical.day.gn"]
    # X = gcm_munge(X)
    # X = X.sel(**domain, time=slice(train_period_start, train_period_end))
    # # coarse_obs_store = fsspec.get_mapper(
    # #     f"az://cmip6/intermediates/{obs_id}_{gcm[0]}_{train_period_start}_{train_period_end}_{variable}.zarr",
    # #     connection_string=connection_string,
    # # )
    # y = coarse_obs.sel(**domain)
    # # finally set the gcm time index to be the same as the obs one (and the era5 index is datetime64 which sklearn prefers)
    # X["time"] = y.time.values
    print('loading xpredict')
    X_predict = load_cmip_dictionary()["ScenarioMIP.MIROC.MIROC6.ssp370.day.gn"]
    print('x_predict loaded')
    X_predict = gcm_munge(X_predict)
    X_predict = X_predict.sel(time=slice(predict_period_start, predict_period_end), **domain)

    # RECHUNKER: X, Y and X_predict rechunked in (lat=10,lon=10,time=-1)
    print(X_predict)
    print(X_predict.tasmax.dims)
    X_predict_rechunked, X_predict_rechunked_path = rechunk_dataset(X_predict, 
                                                            chunks_dict={'tasmax': {'time':-1, 
                                                                                    'lat': 2, 
                                                                                    'lon': 2}}, 
                                                            connection_string=connection_string,
                                                            max_mem='1GB') 
    return X, y, X_predict


@task(log_stdout=True)
def fit_and_predict(X_train, y, X_predict, dim="time", feature_list=["tasmax"]):
    """expects X,y, X_predict to be chunked in (lat=10,lon=10,time=-1)

    Parameters
    ----------
    X_train : [type]
        [description]
    y : [type]
        [description]
    X_predict : [type]
        [description]
    dim : str, optional
        [description], by default "time"
    feature_list : list, optional
        [description], by default ["tasmax"]

    Returns
    -------
    xarray dataset
        Unknown chunking.  Probably (lat=10,lon=10,time=-1)
    """
    bcsd_model = BcAbsolute(return_anoms=False)
    pointwise_model = PointWiseDownscaler(model=bcsd_model, dim=dim)
    pointwise_model.fit(X_train, y)
    bias_corrected = pointwise_model.predict(X_predict)
    # ALTERNATIVE: could use the bcsdwrapper like below:
    # bcsd_wrapper = BcsdWrapper(model=bcsd_model, feature_list=['tasmax'], dim='time')
    # bcsd_wrapper.fit(X=X_train, y=y)
    return bias_corrected


@task(log_stdout=True)
def postprocess_bcsd(
    y_predict,
    gcm,
    obs_id,
    train_period_start,
    train_period_end,
    variable,
    predict_period_start,
    predict_period_end,
):
    """[summary]

    Parameters
    ----------
    y_predict : xarray dataset
        (lat=10,lon=10,time=-1)
    gcm : [type]
        [description]
    obs_id : [type]
        [description]
    train_period_start : [type]
        [description]
    train_period_end : [type]
        [description]
    variable : [type]
        [description]
    predict_period_start : [type]
        [description]
    predict_period_end : [type]
        [description]
    """
    spatial_anomalies_store = fsspec.get_mapper(
        f"az://cmip6/intermediates/anomalies_{obs_id}_{gcm[0]}_{train_period_start}_{train_period_end}_{variable}.zarr",
        connection_string=connection_string,
    )
    # spatial anomalies is chunked in (lat=-1,lon=-1,time=1)
    spatial_anomalies = xr.open_zarr(spatial_anomalies_store, consolidated=True)
    regridder = xe.Regridder(y_predict, spatial_anomalies, "bilinear", extrap_method="nearest_s2d")
    # Rechunk y_predict to (lat=-1,lon=-1,time=1)
    y_predict_fine = regridder(y_predict)
    # This lat=-1,lon=-1,time=1) chunking might be best? Check to make sure that is what is returned from regridder.
    bcsd_results = y_predict_fine.groupby("time.month") + spatial_anomalies
    bcsd_store = fsspec.get_mapper(
        f"az://cmip6/intermediates/bcsd_{obs_id}_{gcm[0]}_{predict_period_start}_{predict_period_end}_{variable}.zarr",
        connection_string=connection_string,
    )
    bcsd_results.to_zarr(bcsd_store, mode="w", consolidated=True)


# # Prefect cloud config settings -----------------------------------------------------------

# run_config = KubernetesRun(
#     cpu_request=2,
#     memory_request="2Gi",
#     image="gcr.io/carbonplan/hub-notebook:7252fc3", #CHANGE
#     labels=["az-eu-west"],
#     env=run_hyperparameters
# )
# Prefect Flow -----------------------------------------------------------
# put the experiment_ids outside of this loop?
with Flow(name=flow_name) as flow:
    # run preprocess and create dependency/checkpoint to show it's done
    obs = Parameter("OBS")
    gcm = Parameter("GCMS")
    train_period_start = Parameter("TRAIN_PERIOD_START")
    train_period_end = Parameter("TRAIN_PERIOD_END")
    predict_period_start = Parameter("PREDICT_PERIOD_START")
    predict_period_end = Parameter("PREDICT_PERIOD_END")
    domain = test_specs["domain"]
    variable = Parameter("VARIABLE")
    # `preprocess` will create the necessary coarsened input files and write them out
    # then we'll read them below

    # task 1.1 read data (era params) - data passed to regridding task
    # task 1.2 read grid (grid params) - data

    # task 2. regridding of data etc ( data, grid, method, tstart, tend)
    # task 3. write task (params)

    # coarse_obs = preprocess_bcsd(
    #     gcm,
    #     obs_id=obs,
    #     train_period_start=train_period_start,
    #     train_period_end=train_period_end,
    #     variable=variable,
    #     out_bucket="cmip6",
    #     domain=domain,
    #     rerun=True,
    # )  # can remove this once we have caching working

    X, y, X_predict = prep_bcsd_inputs(
        # coarse_obs,
        gcm,
        obs_id=obs,
        train_period_start=train_period_start,
        train_period_end=train_period_end,
        predict_period_start=predict_period_start,
        predict_period_end=predict_period_end,
        variable=variable,
        out_bucket="cmip6",
        domain=domain,
    )
    y_predict = fit_and_predict(X, y, X_predict)
    postprocess_bcsd(
        y_predict,
        gcm,
        obs,
        train_period_start,
        train_period_end,
        variable,
        predict_period_start,
        predict_period_end,
    )
# flow.visualize()
flow.run(parameters=run_hyperparameters)


# with Flow(name=flow_name) as flow:
#     ds_dict = test_intake()
#     print(ds_dict)


# for run_hyperparameters in list_of_hyperparameter_dicts:
#     flow.run(parameters=run_hyperparameters)
# make all permutations of list_of_hyperparameter_dicts:

# task.map(list_of_hyperparameter_dicts)