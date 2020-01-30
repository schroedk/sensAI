import logging
from typing import Sequence, List, Union, Callable, Any, Dict
from abc import ABC, abstractmethod

import pandas as pd
import numpy as np

from . import util, data_transformation
from .columngen import ColumnGenerator

log = logging.getLogger(__name__)


class FeatureGenerator(ABC):
    """
    Base class for feature generators that create a new DataFrame containing feature values
    from an input DataFrame
    """
    def __init__(self, categoricalFeatureNames: Sequence[str] = (),
            normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = (), addCategoricalDefaultRules=True):
        """
        :param categoricalFeatureNames: if provided, will be added to the feature generator's meta-information which can
            be leveraged for further transformations, e.g. one-hot encoding.
        :param normalisationRules: Rules to be used by DFTNormalisation (e.g. for constructing an input transformer for a model).
            These rules are only relevant if a DFTNormalisation object consuming them is instantiated and used
            within a data processing pipeline. They do not affect feature generation.
        :param addCategoricalDefaultRules:
            If True, normalisation rules for categorical features (which are unsupported by normalisation) and their corresponding one-hot
            encoded features (with "_<index>" appended) will be added.
        """
        self._categoricalFeatureNames = categoricalFeatureNames
        normalisationRules = list(normalisationRules)
        if addCategoricalDefaultRules and len(categoricalFeatureNames) > 0:
            normalisationRules.append(data_transformation.DFTNormalisation.Rule(r"(%s)" % "|".join(categoricalFeatureNames), unsupported=True))
            normalisationRules.append(data_transformation.DFTNormalisation.Rule(r"(%s)_\d+" % "|".join(categoricalFeatureNames), skip=True))
        log.info(f"Rules {list(map(str, normalisationRules))}")
        self._normalisationRules = normalisationRules

    def getNormalisationRules(self) -> Sequence[data_transformation.DFTNormalisation.Rule]:
        return self._normalisationRules

    def getCategoricalFeatureNames(self) -> Sequence[str]:
        return self._categoricalFeatureNames

    @abstractmethod
    def fit(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None):
        """
        Fits the feature generator based on the given data

        :param X: the input/features data frame for the learning problem
        :param Y: the corresponding output data frame for the learning problem
            (which will typically contain regression or classification target columns)
        :param ctx: a context object whose functionality may be required for feature generation;
            this is typically the model instance that this feature generator is to generate inputs for
        """
        pass

    @abstractmethod
    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        """
        Generates features for the data points in the given data frame

        :param df: the input data frame for which to generate features
        :param ctx: a context object whose functionality may be required for feature generation;
            this is typically the model instance that this feature generator is to generate inputs for
        :return: a data frame containing the generated features, which uses the same index as X (and Y)
        """
        pass

    def fitGenerate(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None) -> pd.DataFrame:
        """
        Fits the feature generator and subsequently generates features for the data points in the given data frame

        :param X: the input data frame for the learning problem and for which to generate features
        :param Y: the corresponding output data frame for the learning problem
            (which will typically contain regression or classification target columns)
        :param ctx: a context object whose functionality may be required for feature generation;
            this is typically the model instance that this feature generator is to generate inputs for
        :return: a data frame containing the generated features, which uses the same index as X (and Y)
        """
        self.fit(X, Y, ctx)
        return self.generate(X, ctx)


class RuleBasedFeatureGenerator(FeatureGenerator, ABC):
    """
    A feature generator which does not require fitting
    """
    def fit(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None):
        pass


class MultiFeatureGenerator(FeatureGenerator):
    def __init__(self, featureGenerators: Sequence[FeatureGenerator]):
        self.featureGenerators = featureGenerators
        categoricalFeatureNames = util.concatSequences([fg.getCategoricalFeatureNames() for fg in featureGenerators])
        normalisationRules = util.concatSequences([fg.getNormalisationRules() for fg in featureGenerators])
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules,
            addCategoricalDefaultRules=False)

    def _generateFromMultiple(self, generateFeatures: Callable[[FeatureGenerator], pd.DataFrame], index) -> pd.DataFrame:
        dfs = []
        for fg in self.featureGenerators:
            df = generateFeatures(fg)
            dfs.append(df)
        if len(dfs) == 0:
            return pd.DataFrame(index=index)
        else:
            return pd.concat(dfs, axis=1)

    def generate(self, inputDF: pd.DataFrame, ctx=None):
        def generateFeatures(fg: FeatureGenerator):
            return fg.generate(inputDF, ctx=ctx)
        return self._generateFromMultiple(generateFeatures, inputDF.index)

    def fitGenerate(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None) -> pd.DataFrame:
        def generateFeatures(fg: FeatureGenerator):
            return fg.fitGenerate(X, Y, ctx)
        return self._generateFromMultiple(generateFeatures, X.index)

    def fit(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None):
        for fg in self.featureGenerators:
            fg.fit(X, Y)


class FeatureGeneratorFromNamedTuples(FeatureGenerator, ABC):
    """
    Generates feature values for one data point at a time, creating a dictionary with
    feature values from each named tuple
    """
    def __init__(self, cache: util.cache.PersistentKeyValueCache = None, categoricalFeatureNames: Sequence[str] = (),
            normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = ()):
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules)
        self.cache = cache

    def generate(self, df: pd.DataFrame, ctx=None):
        dicts = []
        for idx, nt in enumerate(df.itertuples()):
            if idx % 100 == 0:
                log.debug(f"Generating feature via {self.__class__.__name__} for index {idx}")
            value = None
            if self.cache is not None:
                value = self.cache.get(nt.Index)
            if value is None:
                value = self._generateFeatureDict(nt)
                if self.cache is not None:
                    self.cache.set(nt.Index, value)
            dicts.append(value)
        return pd.DataFrame(dicts, index=df.index)

    @abstractmethod
    def _generateFeatureDict(self, namedTuple) -> Dict[str, Any]:
        """
        Creates a dictionary with feature values from a named tuple

        :param namedTuple: the data point for which to generate features
        :return: a dictionary mapping feature names to values
        """
        pass


class FeatureGeneratorTakeColumns(RuleBasedFeatureGenerator):
    def __init__(self, columns: Union[str, List[str]], categoricalFeatureNames: Sequence[str] = (),
            normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = ()):
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules)
        if isinstance(columns, str):
            columns = [columns]
        self.columns = columns

    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        missingCols = set(self.columns) - set(df.columns)
        if len(missingCols) > 0:
            raise Exception(f"Columns {missingCols} not present in data frame; available columns: {list(df.columns)}")
        return df[self.columns]


class FeatureGeneratorTakeAllColumns(RuleBasedFeatureGenerator):
    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        return df


class FeatureGeneratorFlattenColumns(RuleBasedFeatureGenerator):
    """
    Instances of this class take columns with vectors and creates a dataframe with columns containing entries of
    these vectors.

    For example, if columns "vec1", "vec2" contain vectors of dimensions dim1, dim2, a datafrane dim1+dim2 new columns
    will be created. It will contain the columns "vec1_<i1>", "vec2_<i2>" with i1, i2 ranging in (0, dim1), (0, dim2).

    """
    def __init__(self, columns: Union[str, Sequence[str]], categoricalFeatureNames: Sequence[str] = (),
            normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = ()):
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules)
        if isinstance(columns, str):
            columns = [columns]
        self.columns = columns

    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        resultDf = pd.DataFrame(index=df.index)
        for col in self.columns:
            log.info(f"Flattening column {col}")
            values = np.stack(df[col].values)
            if len(values.shape) != 2:
                raise ValueError(f"Column {col} was expected to contain one dimensional vectors, something went wrong")
            dimension = values.shape[1]
            new_columns = [f"{col}_{i}" for i in range(dimension)]
            log.info(f"Adding {len(new_columns)} new columns to feature dataframe")
            resultDf[new_columns] = pd.DataFrame(values, index=df.index)
        return resultDf


class FeatureGeneratorFromColumnGenerator(RuleBasedFeatureGenerator):
    """
    Implements a feature generator via a column generator
    """
    log = log.getChild(__qualname__)

    def __init__(self, columnGen: ColumnGenerator, takeInputColumnIfPresent=False, categoricalFeatureNames: Sequence[str] = (),
            normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = ()):
        """
        :param columnGen: the underlying column generator
        :param takeInputColumnIfPresent: if True, then if a column whose name corresponds to the column to generate exists
            in the input data, simply copy it to generate the output (without using the column generator); if False, always
            apply the columnGen to generate the output
        """
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules)
        self.takeInputColumnIfPresent = takeInputColumnIfPresent
        self.columnGen = columnGen

    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        colName = self.columnGen.columnName
        if self.takeInputColumnIfPresent and colName in df.columns:
            self.log.debug(f"Taking column '{colName}' from input data frame")
            series = df[colName]
        else:
            self.log.debug(f"Generating column '{colName}' via {self.columnGen}")
            series = self.columnGen.generateColumn(df)
        return pd.DataFrame({colName: series})


class ChainedFeatureGenerator(FeatureGenerator):
    """
    Chains feature generators such that they are executed one after another. The output of generator i>=1 is the input of
    generator i+1 in the generator sequence.
    """
    def __init__(self, *featureGenerators: FeatureGenerator, categoricalFeatureNames: Sequence[str] = None,
                 normalisationRules: Sequence[data_transformation.DFTNormalisation.Rule] = None):
        """
        :param featureGenerators: the list of feature generators to apply in order
        :param categoricalFeatureNames: the list of categorical feature names being generated; if None, use the ones
            indicated by the last feature generator in the list
        :param normalisationRules: normalisation rules to use; if None, use rules of the last feature generator in the list
        """
        if len(featureGenerators) == 0:
            raise ValueError("Empty list of feature generators")
        if categoricalFeatureNames is None:
            categoricalFeatureNames = featureGenerators[-1].getCategoricalFeatureNames()
        if normalisationRules is None:
            normalisationRules = featureGenerators[-1].getNormalisationRules()
        super().__init__(categoricalFeatureNames=categoricalFeatureNames, normalisationRules=normalisationRules)
        self.featureGenerators = featureGenerators

    def generate(self, df: pd.DataFrame, ctx=None) -> pd.DataFrame:
        for featureGen in self.featureGenerators:
            df = featureGen.generate(df, ctx)
        return df

    def fit(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None):
        self.fitGenerate(X, Y, ctx)

    def fitGenerate(self, X: pd.DataFrame, Y: pd.DataFrame, ctx=None) -> pd.DataFrame:
        for fg in self.featureGenerators:
            X = fg.fitGenerate(X, Y, ctx)
        return X


################################
#
# generator registry
#
################################


class FeatureGeneratorRegistry:
    """
    Represents a registry for named feature generators which can be instantiated via factories.
    Each named feature generator is a singleton, i.e. each factory will be called at most once.
    """
    def __init__(self):
        self._featureGeneratorFactories = {}
        self._featureGeneratorSingletons = {}

    def registerFactory(self, name, factory: Callable[[], FeatureGenerator]):
        """
        Registers a feature generator factory which can subsequently be referenced by models via their name
        :param name: the name
        :param factory: the factory
        """
        if name in self._featureGeneratorFactories:
            raise ValueError(f"Generator for name '{name}' already registered")
        self._featureGeneratorFactories[name] = factory

    def getFeatureGenerator(self, name):
        """
        Creates a feature generator from a name, which must

        :param name: the name of the generator
        :return: a new feature generator instance
        """
        generator = self._featureGeneratorSingletons.get(name)
        if generator is None:
            factory = self._featureGeneratorFactories.get(name)
            if factory is None:
                raise ValueError(f"No factory registered for name '{name}': known names: {list(self._featureGeneratorFactories.keys())}. Use registerFeatureGeneratorFactory to register a new feature generator factory.")
            generator = factory()
            self._featureGeneratorSingletons[name] = generator
        return generator


class FeatureCollector(object):
    """
    A feature collector which can provide a multi-feature generator from a list of names/generators and registry
    """

    def __init__(self, *featureGeneratorsOrNames: Union[str, FeatureGenerator], registry=None):
        """
        :param featureGeneratorsOrNames: generator names (known to articleFeatureGeneratorRegistry) or generator instances.
        :param registry: the feature generator registry for the case where names are passed
        """
        self._featureGeneratorsOrNames = featureGeneratorsOrNames
        self._registry = registry
        self._multiFeatureGenerator = self.createMultiFeatureGenerator()

    def getMultiFeatureGenerator(self) -> MultiFeatureGenerator:
        return self._multiFeatureGenerator

    def createMultiFeatureGenerator(self):
        featureGenerators = []
        for f in self._featureGeneratorsOrNames:
            if isinstance(f, FeatureGenerator):
                featureGenerators.append(f)
            elif type(f) == str:
                if self._registry is None:
                    raise Exception(f"Received feature name '{f}' instead of instance but no registry to perform the lookup")
                featureGenerators.append(self._registry.getFeatureGenerator(f))
            else:
                raise ValueError(f"Unexpected type {type(f)} in list of features")
        return MultiFeatureGenerator(featureGenerators)