# -*- coding: utf-8 -*-

import os
import glob
import unittest
import logging
import contextlib
import copy
import nbformat
import tempfile
import dateutil.parser
import importlib
import sys

from unittest import mock

import pandas as pd
import numpy as np

from nbconvert.exporters import PythonExporter

from gordo_components.dataset._datasets import DataLakeBackedDataset


logger = logging.getLogger(__name__)


def _fake_data():
    data = pd.DataFrame({f"sensor_{i}": np.random.random(size=100) for i in range(10)})
    return data, data


class ExampleNotebooksTestCase(unittest.TestCase):
    @mock.patch.object(DataLakeBackedDataset, "get_data", return_value=_fake_data())
    def test_faked_DataLakeBackedDataset(self, _mocked_method):

        dataset = DataLakeBackedDataset(
            datalake_config={"storename": "dataplatformdlsprod", "interactive": True},
            from_ts=dateutil.parser.isoparse("2014-07-01T00:10:00+00:00"),
            to_ts=dateutil.parser.isoparse("2015-01-01T00:00:00+00:00"),
            tag_list=[
                "asgb.19ZT3950%2FY%2FPRIM",
                "asgb.19PST3925%2FDispMeasOut%2FPRIM",
            ],
        )

        # Should be able to call get_data without being asked to authenticate in tests
        X, y = dataset.get_data()

    @mock.patch.object(DataLakeBackedDataset, "get_data", return_value=_fake_data())
    def test_notebooks(self, _mocked_method):
        """
        Ensures all notebooks will run without error
        """
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        notebooks = glob.glob(os.path.join(repo_root, "examples", "*.ipynb"))

        logger.info(f"Found {len(notebooks)} notebooks to test.")

        for notebook in notebooks:

            logger.info(f"Testing notebook: {os.path.basename(notebook)}")

            with open(notebook) as f:
                nb = nbformat.read(f, as_version=4)
                exporter = PythonExporter()
                source, _meta = exporter.from_notebook_node(nb)

                with tempfile.TemporaryDirectory() as tmpdir:
                    with open(os.path.join(tmpdir, "tmpmodule.py"), "w") as f:
                        f.writelines(source)
                    with open(os.path.join(tmpdir, "__init__.py"), "w") as f:
                        f.write("from .tmpmodule import *")

                    # Import this module to 'run' the code
                    module_dir = os.path.join(tmpdir, "..")
                    sys.path.insert(0, module_dir)

                    if "TestFail" in notebook:
                        with self.assertRaises(ImportError):
                            importlib.import_module(os.path.basename(tmpdir), ".")
                    else:
                        importlib.import_module(os.path.basename(tmpdir), ".")

                    sys.path.remove(module_dir)