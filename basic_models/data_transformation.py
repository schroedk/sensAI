import copy
import logging
import re
from abc import ABC, abstractmethod
from typing import List, Sequence, Union, Dict, Callable, Any

import numpy as np
import pandas as pd
from sklearn.preprocessing import OneHotEncoder

from .columngen import ColumnGenerator

log = logging.getLogger(__name__)


class DataFrameTransformer(ABC):
    """
    Base class for data frame transformers, i.e. objects which can transform one data frame into another
    (possibly applying the transformation to the original data frame - in-place transformation).
    A data frame transformer may require being fitted using training data.
    """

    @abstractmethod
    def fit(self, df: pd.DataFrame):
        pass

    @abstractmethod
    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        pass


class InvertibleDataFrameTransformer(DataFrameTransformer, ABC):
    @abstractmethod
    def inverse(self) -> DataFrameTransformer:
        pass


class RuleBasedDataFrameTransformer(DataFrameTransformer, ABC):
    """Base class for transformers whose logic is entirely based on rules and does not need to be fitted to data"""

    def fit(self, df: pd.DataFrame):
        pass


class DataFrameTransformerChain:
    """Supports the application of a chain of data frame transformers"""

    def __init__(self, dataFrameTransformers: Sequence[DataFrameTransformer]):
        self.dataFrameTransformers = dataFrameTransformers

    def apply(self, df: pd.DataFrame, fit=False) -> pd.DataFrame:
        """
        Applies the chain of transformers to the given DataFrame, optionally fitting each transformer before applying it.
        Each transformer in the chain receives the transformed output of its predecessor.

        :param df: the data frame
        :param fit: whether to fit the transformers before applying them
        :return: the transformed data frame
        """
        for transformer in self.dataFrameTransformers:
            if fit:
                transformer.fit(df)
            df = transformer.apply(df)
        return df


class DFTRenameColumns(RuleBasedDataFrameTransformer):
    def __init__(self, columnsMap: Dict[str, str]):
        self.columnsMap = columnsMap

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns=self.columnsMap)


class DFTRowFilterOnColumn(RuleBasedDataFrameTransformer):
    def __init__(self, column: str, condition: Callable[[Any], bool]):
        self.column = column
        self.condition = condition

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        return df[df[self.column].apply(self.condition)]


class DFTOneHotEncoder(DataFrameTransformer):
    def __init__(self, columns: Sequence[str], categoriesList: List[np.ndarray] = None):
        """
        One hot encode categorical variables
        :param columns: names of original columns that are to be replaced by a list one-hot encoded columns each
        :param categoriesList: numpy arrays containing the possible values of each of the specified columns.
        If None, the possible values will be inferred from the columns
        """
        self.oneHotEncoders = None
        self.columnNamesToProcess = columns
        if categoriesList is not None:
            if len(columns) != len(categoriesList):
                raise ValueError(f"Length of categories is not the same as length of columnNamesToProcess")
            self.oneHotEncoders = [OneHotEncoder(categories=[np.sort(categories)], sparse=False) for categories in categoriesList]

    def fit(self, df: pd.DataFrame):
        if self.oneHotEncoders is None:
            self.oneHotEncoders = [OneHotEncoder(categories=[np.sort(df[column].unique())], sparse=False) for column in self.columnNamesToProcess]
        for encoder, columnName in zip(self.oneHotEncoders, self.columnNamesToProcess):
            encoder.fit(df[[columnName]])

    def apply(self, df: pd.DataFrame):
        df = df.copy()
        for encoder, columnName in zip(self.oneHotEncoders, self.columnNamesToProcess):
            encodedArray = encoder.transform(df[[columnName]])
            df = df.drop(columns=columnName)
            for i in range(encodedArray.shape[1]):
                df["%s_%d" % (columnName, i)] = encodedArray[:, i]
        return df


class DFTColumnFilter(RuleBasedDataFrameTransformer):
    """
    A DataFrame transformer that filters columns by retaining or dropping specified columns
    """

    def __init__(self, keep: Union[str, Sequence[str]] = None, drop: Union[str, Sequence[str]] = None):
        self.keep = [keep] if type(keep) == str else keep
        self.drop = drop

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if self.keep is not None:
            df = df[self.keep]
        if self.drop is not None:
            df = df.drop(columns=self.drop)
        return df


class DFTNormalisation(DataFrameTransformer):
    """
    Applies normalisation/scaling to a data frame by applying a set of transformation rules, where each
    rule defines a set of columns to which it applies (learning a single transformer based on the values
    of all applicable columns)
    """

    class Rule:
        def __init__(self, regex: str, skip=False, transformer=None):
            """
            :param regex: a regular expression defining the column the rule applies to
            :param skip: whether no transformation shall be performed on the matching column(s)
            :param transformer: a transformer instance (from sklearn.preprocessing, e.g. StandardScaler) to apply to the matching column(s)
            """
            if skip and transformer is not None:
                raise ValueError("skip==True while transformer is not None")
            self.regex = re.compile(regex)
            self.skip = skip
            self.transformer = transformer

        def matches(self, column: str):
            return self.regex.fullmatch(column) is not None

        def matchingColumns(self, columns: Sequence[str]):
            return [col for col in columns if self.matches(col)]

        def __str__(self):
            return f"{self.__class__.__name__}[{self.regex}]"

    def __init__(self, rules: Sequence[Rule], defaultTransformerFactory=None, requireAllHandled=True, inplace=True):
        """
        :param rules: the set of rules to apply
        :param defaultTransformerFactory: a factory for the creation of transformer instances (from sklearn.preprocessing, e.g. StandardScaler)
            that shall be used to create a transformer for all rules that don't specify a particular transformer
        :param requireAllHandled: whether to raise an exception if not all columns are matched by a rule
        :param inplace: whether to apply data frame transformations in-place
        """
        self.requireAllHandled = requireAllHandled
        self.inplace = inplace
        self._userRules = rules
        self._defaultTransformerFactory = defaultTransformerFactory
        self._rules = None

    def fit(self, df: pd.DataFrame):
        matchedRulesByColumn = {}
        self._rules = []
        for rule in self._userRules:
            matchingColumns = rule.matchingColumns(df.columns)
            for c in matchingColumns:
                if c in matchedRulesByColumn:
                    raise Exception(f"More than one rule applies to column '{c}': {matchedRulesByColumn[c]}, {rule}")
                matchedRulesByColumn[c] = rule

            # fit transformer
            if len(matchingColumns) > 0:
                if not rule.skip:
                    if rule.transformer is None:
                        if self._defaultTransformerFactory is None:
                            raise Exception(f"No transformer to fit: {rule} defines no transformer and instance has no transformer factory")
                        rule.transformer = self._defaultTransformerFactory()
                    applicableDF = df[matchingColumns]
                    flatValues = applicableDF.values.flatten()
                    rule.transformer.fit(flatValues.reshape((len(flatValues), 1)))
            else:
                log.debug(f"{rule} matched no columns")

            # collect specialised rule for application
            specialisedRule = copy.copy(rule)
            try:
                r = "|".join([re.escape(colName) for colName in matchingColumns])
                specialisedRule.regex = re.compile(r)
            except Exception as e:
                raise Exception(f"Could not compile regex '{r}': {e}")
            self._rules.append(specialisedRule)

    def _checkUnhandledColumns(self, df, matchedRulesByColumn):
        if self.requireAllHandled:
            unhandledColumns = set(df.columns) - set(matchedRulesByColumn.keys())
            if len(unhandledColumns) > 0:
                raise Exception(f"The following columns are not handled by any rules: {unhandledColumns}")

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.inplace:
            df = df.copy()
        matchedRulesByColumn = {}
        for rule in self._rules:
            for c in rule.matchingColumns(df.columns):
                matchedRulesByColumn[c] = rule
                if not rule.skip:
                    df[c] = rule.transformer.transform(df[[c]].values)
        self._checkUnhandledColumns(df, matchedRulesByColumn)
        return df


class DFTFromColumnGenerators(RuleBasedDataFrameTransformer):
    def __init__(self, columnGenerators: Sequence[ColumnGenerator]):
        self.columnGenerators = columnGenerators

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        for cg in self.columnGenerators:
            series = cg.generateColumn(df)
            df[series.name] = series
        return df


class DFTCountEntries(RuleBasedDataFrameTransformer):
    def __init__(self, columnForEntryCount: str, columnNameForResultingCounts: str = "counts"):
        self.columnNameForResultingCounts = columnNameForResultingCounts
        self.columnForEntryCount = columnForEntryCount

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        series = df[self.columnForEntryCount].value_counts()
        return pd.DataFrame({self.columnForEntryCount: series.index, self.columnNameForResultingCounts: series.values})


class DFTSkLearnTransformer(InvertibleDataFrameTransformer):
    def __init__(self, sklearnTransformer, columns=None):
        self.sklearnTransformer = sklearnTransformer
        self.columns = columns
        self._isInverse = False

    def fit(self, df: pd.DataFrame):
        cols = self.columns
        if cols is None:
            cols = df.columns
        self.sklearnTransformer.fit(df[cols].values)

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        cols = self.columns
        if cols is None:
            cols = df.columns
        if self._isInverse:
            df[cols] = self.sklearnTransformer.inverse_transform(df[cols].values)
        else:
            df[cols] = self.sklearnTransformer.transform(df[cols].values)
        return df

    def inverse(self) -> DataFrameTransformer:
        dft = copy.copy(self)
        dft._isInverse = True
        return dft
