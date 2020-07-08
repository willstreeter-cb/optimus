import dask
import dask.dataframe as dd
import numpy as np
import pandas as pd
from dask import delayed
from dask_ml import preprocessing
from sklearn.preprocessing import MinMaxScaler

from optimus.engines.base.columns import BaseColumns
from optimus.engines.base.ml.contants import INDEX_TO_STRING
from optimus.helpers.columns import parse_columns, get_output_cols, \
    prepare_columns
from optimus.helpers.constants import Actions
from optimus.helpers.raiseit import RaiseIt
from optimus.infer import Infer
from optimus.profiler.functions import fill_missing_var_types

MAX_BUCKETS = 33

TOTAL_PREVIEW_ROWS = 30


class DaskBaseColumns(BaseColumns):

    def __init__(self, df):
        super(DaskBaseColumns, self).__init__(df)

    @staticmethod
    def exec_agg(exprs, compute):
        """
        Execute and aggregation
        :param exprs:
        :return:
        """
        if compute is True:
            result = exprs.compute()
        else:
            result = exprs
        return result

    def append(self, dfs):
        """

        :param dfs:
        :return:
        """

        df = self.df
        df = dd.concat([dfs.reset_index(drop=True), df.reset_index(drop=True)], axis=1)
        return df

    # def count_mismatch(self, columns_mismatch: dict = None, compute=True):
    #     df = self.df
    #     if not is_dict(columns_mismatch):
    #         columns_mismatch = parse_columns(df, columns_mismatch)
    #     init = {0: 0, 1: 0, 2: 0}
    #
    #     @delayed
    #     def count_dtypes(_df, _col_name, _func_dtype):
    #
    #         def _func(value):
    #
    #             # match data type
    #             if _func_dtype(value):
    #                 # ProfilerDataTypesQuality.MATCH.value
    #                 return 2
    #
    #             elif pd.isnull(value):
    #                 # ProfilerDataTypesQuality.MISSING.value
    #                 return 1
    #
    #             # mismatch
    #             else:
    #                 # ProfilerDataTypesQuality.MISMATCH.value
    #                 return 0
    #
    #         r = _df[_col_name].astype(str).map(_func).value_counts().ext.to_dict()
    #
    #         r = update_dict(init.copy(), r)
    #         a = {_col_name: {"mismatch": r[0], "missing": r[1], "match": r[2]}}
    #         return a
    #
    #     partitions = df.to_delayed()
    #
    #     delayed_parts = [count_dtypes(part, col_name, profiler_dtype_func(dtype, True)) for part in
    #                      partitions for col_name, dtype in columns_mismatch.items()]
    #
    #     @delayed
    #     def merge(_pdf):
    #         columns = set(list(i.keys())[0] for i in _pdf)
    #         r = {col_name: {"mismatch": 0, "missing": 0, "match": 0} for col_name in columns}
    #
    #         for l in _pdf:
    #             for i, j in l.items():
    #                 r[i]["mismatch"] = r[i]["mismatch"] + j["mismatch"]
    #                 r[i]["missing"] = r[i]["missing"] + j["missing"]
    #                 r[i]["match"] = r[i]["match"] + j["match"]
    #
    #         return r
    #
    #     # TODO: Maybe we can use a reduction here https://docs.dask.org/en/latest/dataframe-api.html#dask.dataframe.Series.reduction
    #     b = merge(delayed_parts)
    #
    #     if compute is True:
    #         result = dd.compute(b)[0]
    #     else:
    #         result = b
    #     return result

    def hist(self, columns, buckets=20, compute=True):

        df = self.df
        columns = parse_columns(df, columns)

        @delayed(df)
        def _bins_col(_columns, _min, _max):
            return {col_name: list(np.linspace(_min["min"][col_name], _max["max"][col_name], num=buckets)) for col_name
                    in
                    _columns}

        _min = df.cols.min(columns, compute=False, tidy=False)
        _max = df.cols.max(columns, compute=False, tidy=False)
        _bins = _bins_col(columns, _min, _max)

        @delayed(df)
        def _hist(pdf, col_name, _bins):
            _count, bins_edges = np.histogram(pdf[col_name].ext.to_float(), bins=_bins[col_name])
            return {col_name: [list(_count), list(bins_edges)]}

        @delayed(df)
        def _agg_hist(values):
            _result = {}
            x = np.zeros(buckets - 1)
            for i in values:
                for j in i:
                    t = i.get(j)
                    if t is not None:
                        _count = np.sum([x, t[0]], axis=0)
                        _bins = t[1]
                        col_name = j
                l = len(_count)
                r = [{"lower": float(_bins[i]), "upper": float(_bins[i + 1]),
                      "count": int(_count[i])} for i in range(l)]
                _result[col_name] = {"hist": r}

            return _result

        partitions = df.to_delayed()
        c = [_hist(part, col_name, _bins) for part in partitions for col_name in columns]

        d = _agg_hist(c)

        if compute is True:
            result = d.compute()
        else:
            result = d
        return result

    def qcut(self, columns, num_buckets, handle_invalid="skip"):

        df = self.df
        columns = parse_columns(df, columns)
        # s.fillna(np.nan)
        df[columns] = df[columns].map_partitions(pd.qcut, num_buckets)
        return df

    def index_to_string(self, input_cols=None, output_cols=None, columns=None):
        df = self.df
        columns = prepare_columns(df, input_cols, output_cols, default=INDEX_TO_STRING, accepts_missing_cols=True)
        le = preprocessing.LabelEncoder()
        kw_columns = {}
        for input_col, output_col in columns:
            kw_columns[output_col] = le.inverse_transform(df[input_col])
        df = df.assign(**kw_columns)

        df = df.meta.preserve(df, Actions.INDEX_TO_STRING.value, output_cols)

        return df

    def string_to_index(self, input_cols=None, output_cols=None, columns=None):

        """
        Encodes a string column of labels to a column of label indices
        :param input_cols:
        :param output_cols:
        :param columns:
        :return:
        """
        df = self.df
        le = preprocessing.LabelEncoder()

        def _string_to_index(value, args):
            return le.fit_transform(value.astype(str))

        return df.cols.apply(input_cols, _string_to_index, func_return_type=str,
                             output_cols=output_cols,
                             meta_action=Actions.STRING_TO_INDEX.value, mode="vectorized")

    @staticmethod
    def correlation(input_cols, method="pearson", output="json"):
        pass

    @staticmethod
    def scatter(columns, buckets=10):
        pass

    @staticmethod
    def standard_scaler():
        pass

    @staticmethod
    def max_abs_scaler(input_cols, output_cols=None):
        pass

    def min_max_scaler(self, input_cols, output_cols=None):
        # https://github.com/dask/dask/issues/2690

        df = self.df

        scaler = MinMaxScaler()

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        # _df = df[input_cols]
        scaler.fit(df[input_cols])
        # print(type(scaler.transform(_df)))
        arr = scaler.transform(df[input_cols])
        darr = dd.from_array(arr)
        # print(type(darr))
        darr.name = 'z'
        df = df.merge(darr)

        return df

    # Date operations

    @staticmethod
    def to_timestamp(input_cols, date_format=None, output_cols=None):
        pass

    # def date_format(self, input_cols, current_format=None, output_format=None, output_cols=None):
    #     """
    #     Look at https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes for date formats
    #     :param input_cols:
    #     :param current_format:
    #     :param output_format:
    #     :param output_cols:
    #     :return:
    #     """
    #     df = self.df
    #
    #     def _date_format(value, args):
    #         return pd.to_datetime(value, format=current_format, errors="coerce").dt.strftime(output_format)
    #
    #     return df.cols.apply(input_cols, _date_format, func_return_type=str, output_cols=output_cols,
    #                          meta_action=Actions.DATE_FORMAT.value, mode="pandas", set_index=True)

    def replace_regex(self, input_cols, regex=None, value=None, output_cols=None):
        """
        Use a Regex to replace values
        :param input_cols: '*', list of columns names or a single column name.
        :param output_cols:
        :param regex: values to look at to be replaced
        :param value: new value to replace the old one
        :return:
        """

        df = self.df

        def _replace_regex(value, regex, replace):
            return value.replace(regex, replace)

        return df.cols.apply(input_cols, func=_replace_regex, args=[regex, value], output_cols=output_cols,
                             filter_col_by_dtypes=df.constants.STRING_TYPES + df.constants.NUMERIC_TYPES)

    def remove_accents(self, input_cols, output_cols=None):

        def _remove_accents(value, args):
            return value.str.normalize("NFKD")

        df = self.df
        return df.cols.apply(input_cols, _remove_accents, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols, mode="pandas", set_index=True)

    def reverse(self, input_cols, output_cols=None):
        def _reverse(value, args):
            return value.astype(str).str[::-1]

        df = self.df
        return df.cols.apply(input_cols, _reverse, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols, mode="pandas", set_index=True)

    @staticmethod
    def astype(*args, **kwargs):
        pass

    @staticmethod
    def apply_by_dtypes(columns, func, func_return_type, args=None, func_type=None, data_type=None):
        pass

    # TODO: Check if we must use * to select all the columns

    def count_by_dtypes(self, columns, infer=False, str_funcs=None, int_funcs=None, mismatch=None):
        df = self.df
        columns = parse_columns(df, columns)
        columns_dtypes = df.cols.dtypes()

        def value_counts(series):
            return series.value_counts()

        delayed_results = []

        for col_name in columns:
            a = df.map_partitions(lambda df: df[col_name].apply(
                lambda row: Infer.parse((col_name, row), infer, columns_dtypes, str_funcs, int_funcs,
                                        full=False))).compute()

            f = df.functions.map_delayed(a, value_counts)
            delayed_results.append({col_name: f.to_dict()})

        results_compute = dask.compute(*delayed_results)
        result = {}

        # Convert list to dict
        for i in results_compute:
            result.update(i)

        if infer is True:
            result = fill_missing_var_types(result, columns_dtypes)
        else:
            result = self.parse_profiler_dtypes(result)

        return result

    def kurtosis(self, columns):
        raise NotImplementedError("Not implemented yet")

    def skewness(self, columns):
        raise NotImplementedError("Not implemented yet")

    def nest(self, input_cols, shape="string", separator="", output_col=None):
        """
        Merge multiple columns with the format specified
        :param input_cols: columns to be nested
        :param separator: char to be used as separator at the concat time
        :param shape: final data type, 'array', 'string' or 'vector'
        :param output_col:
        :return: Dask DataFrame
        """

        df = self.df
        input_cols = parse_columns(df, input_cols)
        # output_col = val_to_list(output_col)
        # check_column_numbers(input_cols, 2)
        if output_col is None:
            RaiseIt.type_error(output_col, ["str"])

        # output_col = parse_columns(df, output_col, accepts_missing_cols=True)

        output_ordered_columns = df.cols.names()

        def _nest_string(row):
            v = row[input_cols[0]].astype(str)
            for i in range(1, len(input_cols)):
                v = v + separator + row[input_cols[i]].astype(str)
            return v

        def _nest_array(row):
            # https://stackoverflow.com/questions/43898035/pandas-combine-column-values-into-a-list-in-a-new-column/43898233
            # t['combined'] = t.values.tolist()

            v = row[input_cols[0]].astype(str)
            for i in range(1, len(input_cols)):
                v += ", " + row[input_cols[i]].astype(str)
            return "[" + v + "]"

        if shape == "string":
            kw_columns = {output_col: _nest_string}
        else:
            kw_columns = {output_col: _nest_array}

        df = df.assign(**kw_columns)

        col_index = output_ordered_columns.index(input_cols[-1]) + 1
        output_ordered_columns[col_index:col_index] = [output_col]

        df = df.meta.preserve(df, Actions.NEST.value, list(kw_columns.values()))

        return df.cols.select(output_ordered_columns)

    def is_numeric(self, col_name):
        """
        Check if a column is numeric
        :param col_name:
        :return:
        """
        df = self.df
        # TODO: Check if this is the best way to check the data type
        if np.dtype(df[col_name]).type in [np.int64, np.int32, np.float64]:
            result = True
        else:
            result = False
        return result
