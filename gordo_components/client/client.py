# -*- coding: utf-8 -*-

import sys  # noqa
import asyncio
import requests
import logging

import typing
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator

from gordo_components import serializer
from gordo_components.client import io as gordo_io
from gordo_components.client.forwarders import PredictionForwarder
from gordo_components.client.utils import EndpointMetadata, PredictionResult
from gordo_components.dataset.datasets import TimeSeriesDataset
from gordo_components.data_provider.base import GordoBaseDataProvider
from gordo_components.dataset.sensor_tag import normalize_sensor_tags

logger = logging.getLogger(__name__)


class Client:
    """
    Basic client shipped with Gordo

    Enables some basic communication with a deployed Gordo project
    """

    def __init__(
        self,
        project: str,
        target: typing.Optional[str] = None,
        host: str = "localhost",
        port: int = 443,
        scheme: str = "https",
        gordo_version: str = "v0",
        metadata: typing.Optional[dict] = None,
        data_provider: typing.Optional[GordoBaseDataProvider] = None,
        prediction_forwarder: typing.Optional[PredictionForwarder] = None,
        batch_size: int = 1000,
        parallelism: int = 10,
        forward_resampled_sensors: bool = False,
    ):
        """

        Parameters
        ----------
        project: str
            Name of the project.
        target: Optional[str]
            Target name if desired to only make predictions against one target.
            Leave as None to run predictions against all targets in Watchman.
        host: str
            Host of where to find watchman and other services.
        port: int
            Port to communicate on.
        scheme: str
            The request scheme to use, ie 'https'.
        gordo_version: str
            The version of major gordo the services are using, ie. 'v0'.
        metadata: Optional[dict]
            Arbitrary mapping of key-value pairs to save to influx with
            prediction runs in 'tags' property
        data_provider: Optional[GordoBaseDataProvider]
            The data provider to use for the dataset. If not set, the client
            will fall back to using the GET /prediction endpoint
        prediction_forwarder: Optional[Callable[[pd.DataFrame, EndpointMetadata, dict, pd.DataFrame], typing.Awaitable[None]]]
            Async callable which will take a dataframe of predictions,
            ``EndpointMetadata``, the metadata, and the dataframe of resampled sensor
            values and forward them somewhere.
        batch_size: int
            How many samples to send to the server, only applicable when data
            provider is supplied.
        parallelism: int
            The maximum number of async tasks to run at a given time when
            running predictions
       forward_resampled_sensors : bool
            If true then forward resampled sensor values to the prediction_forwarder

        """

        self.base_url = f"{scheme}://{host}:{port}"
        self.watchman_endpoint = f"{self.base_url}/gordo/{gordo_version}/{project}/"
        self.metadata = metadata if metadata is not None else dict()
        self.endpoints = self._endpoints_from_watchman(self.watchman_endpoint)
        self.prediction_forwarder = prediction_forwarder
        self.data_provider = data_provider
        self.batch_size = batch_size
        self.parallelism = parallelism
        self.forward_resampled_sensors = forward_resampled_sensors

        # Filter down to single endpoint if requested
        if target:
            endpoints = [ep for ep in self.endpoints if ep.target_name == target]
            if not endpoints:
                raise ValueError(
                    f"Target name not found in available targets: {endpoints}"
                )
            if len(endpoints) > 1:  # This should never happen...
                raise ValueError(
                    f"Found multiple endpoints with same target name: {endpoints}"
                )
            self.endpoints = endpoints

    def _endpoints_from_watchman(self, endpoint: str) -> typing.List[EndpointMetadata]:
        """
        Get a list of endpoints by querying Watchman
        """
        resp = requests.get(endpoint)
        if not resp.ok:
            raise IOError(f"Failed to get endpoints: {resp.content}")
        return [
            EndpointMetadata(
                target_name=data["metadata"]["metadata"]["user-defined"][
                    "machine-name"
                ],
                healthy=data["healthy"],
                endpoint=f'{self.base_url}{data["endpoint"].rstrip("/")}',
                tag_list=normalize_sensor_tags(
                    data["metadata"]["metadata"]["dataset"]["tag_list"]
                ),
                resolution=data["metadata"]["metadata"]["dataset"]["resolution"],
            )
            for data in resp.json()["endpoints"]
        ]

    def download_model(self) -> typing.Dict[str, BaseEstimator]:
        """
        Download the actual model(s) from the ML server /download-model

        Returns
        -------
        Dict[str, BaseEstimator]
            Mapping of target name to the model
        """
        models = dict()
        for endpoint in self.endpoints:
            resp = requests.get(f"{endpoint.endpoint}/download-model")
            if resp.ok:
                models[endpoint.target_name] = serializer.loads(resp.content)
            else:
                raise IOError(f"Failed to download model: '{resp.content}'")
        return models

    def get_metadata(self) -> typing.Dict[str, dict]:
        """
        Get the metadata for each target

        Parameters
        ----------
        target: str
            Name of the machine/target to get metadata from

        Returns
        -------
        Dict[str, dict]
            Mapping of target names to their metadata
        """
        metadata = dict()
        for endpoint in self.endpoints:
            resp = requests.get(f"{endpoint.endpoint}/metadata")
            if resp.ok:
                metadata[endpoint.target_name] = resp.json()
            else:
                raise IOError(f"Failed to get metadata: '{resp.content}'")
        return metadata

    def predict(
        self, start: datetime, end: datetime
    ) -> typing.Iterable[typing.Tuple[str, pd.DataFrame, typing.List[str]]]:
        """
        Start the prediction process. Will perform POST prediction workflow if
        Client has a data_provider instance, otherwise default to GET based predictions

        Parameters
        ----------
        start: datetime
        end: datetime

        Returns
        -------
        List[Tuple[str, pandas.core.DataFrame, List[str]]
            A list of tuples, where:
              0th element is the target name
              1st element is the dataframe of the predictions; complete with a DateTime index.
              2nd element is a list of error messages (if any) for running the predictions
        """

        # Determine which method we'll use to get predictions
        # If we don't have a data_provider instance, we can't source our own data.
        if self.data_provider is not None:
            predict_method = self._predict_via_post
        else:
            predict_method = self._predict_via_get

        # For every endpoint, start making predictions for the time range
        jobs = asyncio.gather(
            *[
                predict_method(endpoint=endpoint, start=start, end=end)
                for endpoint in self.endpoints
            ]
        )

        # Create new event loop and process getting predictions
        loop = asyncio.get_event_loop()
        prediction_results = loop.run_until_complete(
            jobs
        )  # type: typing.List[PredictionResult]

        # List of tuples where each represents a single target of name, dataframe of predictions
        return [
            (pr.name, pr.predictions, pr.error_messages) for pr in prediction_results
        ]  # type: ignore

    async def _predict_via_post(
        self, endpoint: EndpointMetadata, start: datetime, end: datetime
    ) -> PredictionResult:
        """
        Get predictions based on the /prediction POST endpoint of Gordo ML Servers

        Parameters
        ----------
        endpoint: EndpointMetadata
            Named tuple which has 'endpoint' specifying the full url to the base ml server
        start: datetime
        end: datetime

        Returns
        -------
        dict
            Prediction response from /prediction GET
        """

        # Fetch all of the raw data
        X, y = await self._raw_data(endpoint, start, end)

        # Forward sensor data
        if self.prediction_forwarder is not None and self.forward_resampled_sensors:
            await self.prediction_forwarder(resampled_sensor_data=X)

        async with aiohttp.ClientSession() as session:

            # Chunk over the dataframe by batch_size
            jobs = [
                self._process_post_prediction_task(
                    X,
                    chunk=slice(i, i + self.batch_size),
                    endpoint=endpoint,
                    start=start,
                    end=end,
                    session=session,
                )
                for i in range(0, X.shape[0], self.batch_size)
            ]
            return await self._accumulate_coroutine_predictions(endpoint, jobs)

    async def _process_post_prediction_task(
        self,
        X: pd.DataFrame,
        chunk: slice,
        endpoint: EndpointMetadata,
        start: datetime,
        end: datetime,
        session: typing.Optional[aiohttp.ClientSession] = None,
    ):
        """
        Post a slice of data to the endpoint

        Parameters
        ----------
        X: pandas.core.DataFrame
            The data for the model, in pandas representation
        chunk: slice
            The slice to take from DataFrame.iloc for the batch size
        endpoint: EndpointMetadata
        start: datetime
        end: datetime

        Notes
        -----
        PredictionResult.predictions may be None if the prediction process fails

        Returns
        -------
        PredictionResult
        """
        # Submit raw data for predictions
        try:
            resp = await gordo_io.post_json(
                f"{endpoint.endpoint}/prediction",
                session=session,
                json={"X": X.iloc[chunk].values.tolist()},
            )
        except IOError as exc:
            msg = (
                f"Failed to get predictions for dates {start} -> {end} "
                f"for target: {endpoint.target_name} "
                f"Error: {exc}"
            )
            logger.error(msg)
            return PredictionResult(
                name=endpoint.target_name, predictions=None, error_messages=[msg]
            )

        # Get the output values
        values = np.array(resp["output"])

        # Chunks can have None as end-point
        chunk_stop = chunk.stop if chunk.stop else len(X)
        # Chunks can also be larger than the actual data
        chunk_stop = min(chunk_stop, len(X))
        predictions = pd.DataFrame(
            data=values,
            columns=[f"input_{sensor}" for sensor in X.columns]
            + [f"output_{sensor}" for sensor in X.columns],
            # match any offsetting from windowed models
            index=X.index[chunk_stop - len(values) : chunk_stop],
        )

        # Forward predictions to any other consumer if registered.
        if self.prediction_forwarder is not None:
            await self.prediction_forwarder(
                predictions=predictions, endpoint=endpoint, metadata=self.metadata
            )

        return PredictionResult(
            name=endpoint.target_name, predictions=predictions, error_messages=[]
        )

    async def _predict_via_get(
        self, endpoint: EndpointMetadata, start: datetime, end: datetime
    ) -> PredictionResult:
        """
        Get predictions based on the /prediction GET endpoint of Gordo ML Servers

        Parameters
        ----------
        endpoint: EndpointMetadata
            Named tuple which has 'endpoint' specifying the full url to the base ml server
        start: datetime
        end: datetime

        Returns
        -------
        dict
            Prediction response from /prediction GET
        """
        start_end_dates = make_date_ranges(start, end, max_interval_days=1, freq="23H")

        async with aiohttp.ClientSession() as session:

            # Create all the jobs which will be done, but don't await them
            jobs = [
                self._process_get_prediction_task(endpoint, start, end, session)
                for start, end in start_end_dates
            ]

            return await self._accumulate_coroutine_predictions(endpoint, jobs)

    async def _process_get_prediction_task(
        self,
        endpoint: EndpointMetadata,
        start: datetime,
        end: datetime,
        session: typing.Optional[aiohttp.ClientSession] = None,
    ):
        """
        Process a single prediction GET request. Will ask /prediction GET endpoint
        for predictions given start and end dates, and create a dataframe of the
        returned results and create one PredictionResult for that time span.

        Parameters
        ----------
        endpoint: EndpointMetadata
        start: datetime
        end: datetime

        Notes
        -----
        PredictionResult.predictions may be None if the prediction process fails

        Returns
        -------
        PredictionResult
        """
        try:
            response = await gordo_io.fetch_json(
                f"{endpoint.endpoint}/prediction",
                session=session,
                json={"start": start.isoformat(), "end": end.isoformat()},
            )
        except IOError as exc:
            msg = (
                f"Failed to get predictions for dates {start} -> {end} "
                f"for target: {endpoint.target_name} "
                f"Error: {exc}"
            )
            logger.error(msg)
            return PredictionResult(
                name=endpoint.target_name, predictions=None, error_messages=[msg]
            )

        logger.info(f"Processing {start} -> {end}")

        # Unpack each record into a flat record where keys will become columns, where a single record looks like:
        # {'start': isoformatdate, 'end': isoformatdate, 'tags': {'tag': float, ...}, 'total_anomaly': float}
        records = list()
        for record in response["output"]:
            # Flatten out 'tags' dict so each key gets its own column
            record.update({k: v for k, v in record.pop("tags").items()})

            # Time parsing
            record.update({"time": pd.to_datetime(record.pop("start"))})

            # Forget about end time
            record.pop("end")

            records.append(record)

        # Convert to dataframe
        predictions = pd.DataFrame.from_records(records, index="time")

        if self.prediction_forwarder is not None:
            await self.prediction_forwarder(
                predictions=predictions, endpoint=endpoint, metadata=self.metadata
            )
        return PredictionResult(
            name=endpoint.target_name, predictions=predictions, error_messages=[]
        )

    async def _accumulate_coroutine_predictions(
        self, endpoint: EndpointMetadata, jobs: typing.List[typing.Coroutine]
    ) -> PredictionResult:
        """
        Take a list of un-awaited async prediction coroutines and return
        a single PredictionResult

        Parameters
        ----------
        endpoint: Endpoint
        jobs: List[Coroutine]
            An awaitable coroutine which will return a single PredictionResult
            from a single prediction task

        Returns
        -------
        PredictionResult
            The accumulated PredictionResult for an endpoint
        """
        prediction_dfs = list()
        error_messages = []  # type: typing.List[str]
        for i in range(0, len(jobs), self.parallelism):

            for prediction_result in await asyncio.gather(
                *jobs[i : i + self.parallelism]
            ):
                if prediction_result.predictions is not None:
                    prediction_dfs.append(prediction_result.predictions)
                error_messages.extend(prediction_result.error_messages)

        predictions = (
            pd.concat(prediction_dfs).sort_index() if prediction_dfs else pd.DataFrame()
        )

        return PredictionResult(
            name=endpoint.target_name,
            predictions=predictions,
            error_messages=error_messages,
        )

    async def _raw_data(
        self, endpoint: EndpointMetadata, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """
        Fetch the required raw data in this time range which would
        satisfy this endpoint's /prediction POST

        Parameters
        ----------
        endpoint: EndpointMetadata
            Named tuple representing the endpoint info from Watchman
        start: datetime
        end: datetime

        Returns
        -------
        pandas.core.DataFrame
            Dataframe of required tags and index reflecting the datetime point
        """
        freq = pd.tseries.frequencies.to_offset(endpoint.resolution)

        dataset = TimeSeriesDataset(  # type: ignore
            data_provider=self.data_provider,
            from_ts=start - freq.delta,
            to_ts=end,
            resolution=endpoint.resolution,
            tag_list=endpoint.tag_list,
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dataset.get_data)
            return await asyncio.wrap_future(future)


def make_date_ranges(
    start: datetime, end: datetime, max_interval_days: int, freq: str = "H"
):
    """
    Split start and end datetimes into a list of datetime intervals.
    If the interval between start and end is less than ``max_interval_days`` then
    the resulting list will contain the original start & end. ie. [(start, end)]

    Otherwise it will split the intervals by ``freq``, parse-able by pandas.

    Parameters
    ----------
    start: datetime
    end: datetime
    max_interval_days: int
        Maximum days between start and end before splitting into intervals
    freq: str
        String frequency parse-able by Pandas

    Returns
    -------
    List[Tuple[datetime, datetime]]
    """
    if (end - start).days >= max_interval_days:
        # Split into 1hr data ranges
        date_range = pd.date_range(start, end, freq=freq)
        return [
            (date_range[i], date_range[i + 1]) for i in range(0, len(date_range) - 1)
        ]
    else:
        return [(start, end)]
