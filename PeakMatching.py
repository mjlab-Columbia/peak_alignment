"""This module contains the functions for peak matching and alignment between conditions and PTMs.
NOTE: This script is most efficiently run in its entirety due to multiprocessing."""

#import libraries
import click
import pandas as pd
import numpy as np
import time
import os
import sqlite3
import multiprocessing
import warnings

class SQLitePeakMatchingManager:
    """Class to manage peak matching data in SQLite database."""

    def __init__(self, db_filename, peak_table_name='peak_table'):
        """Initialize the SQLitePeakMatchingManager object.
        
        Args:
            db_filename (str): The filename of the SQLite database.
            peak_table_name (str): The name of the table containing the peak data.
        """
        self.db_filename = db_filename
        self.db_folder_path = os.path.dirname(db_filename)
        self.conn = sqlite3.connect(db_filename)

        #import and prepare peak table
        self.peak_table = pd.read_sql_query(f"SELECT * FROM {peak_table_name};", self.conn)
        self.peak_table = self.peak_table.drop(columns='sample')
        combine_cols = ['protein_id', 'cluster', 'replicate']
        group_cols = ['match_id', 'protein_id', 'genes', 'cluster', 'replicate']

        self.conditions = self.peak_table['condition'].unique()
        self.ptms = self.peak_table['ptm'].unique()
        self.replicates = self.peak_table['replicate'].unique()

        #split peak table into comparison groups
        if len(self.replicates) == 1:
            combine_cols.remove('replicate')
            group_cols.remove('replicate')
        
        if '0' in self.peak_table['cluster'].values:
            combine_cols.remove('cluster')
            group_cols.remove('cluster')

        self.peak_table['match_id'] = self.peak_table[combine_cols].astype(str).agg('_'.join, axis=1)
        
        self.match_id = self.peak_table['match_id'].unique()
        self.match_df_list = [group for _, group in self.peak_table.groupby('match_id')]

        self.group_cols = group_cols

    
    def save_to_SQL(self, df, table_name):
        """Save a DataFrame to a table in the SQLite database.
        
        Args:
            df (pd.DataFrame): The DataFrame to save.
            table_name (str): The name of the table to save the DataFrame to.
        """
        #convert lists to string
        def convert_lists_to_string(cell):
            if isinstance(cell, list):
                return '|'.join(map(str, cell))
            elif pd.isna(cell):
                return cell
            else:
                return str(cell)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            df = df.applymap(convert_lists_to_string) #ignore this warning for now

        # Register adapters for other non-supported types if needed
        sqlite3.register_adapter(np.int64, lambda val: int(val))
        sqlite3.register_adapter(np.int32, lambda val: int(val))

        # Save to SQLite
        df.to_sql(table_name, self.conn, index=False, if_exists='replace')
        self.conn.commit()

    def close_connection(self):
        """Close the connection to the SQLite database."""
        self.conn.close()


###PTM peak matching functions - performed separately in this iteration###
def ptm_eval(cond_ptm_merge_df, cond_ptm='_ptm', apex_diff_cutoff=1):
    """Evaluate the peak matching between the global and ptm peaks.
     This version tests whether: 
     (1)the peak difference is within the distance cutoff, and 
     (2) whether the peaks left/right bounds overlap.
    
    Args:
        cond_ptm_merge_df (pd.DataFrame): The DataFrame containing the global and ptm peaks.
        cond_ptm (str): The suffix for the ptm columns (Default: '_ptm').
        apex_diff_cutoff (int): The maximum difference in peak apexes allowed for a match (Default: 1).
    
    Returns:
        pd.Series: A series of 1s and 0s indicating whether the peak matches the ptm peak.
    """
    #apex condition
    peak_apex_diff = abs(cond_ptm_merge_df['peak_apex'] - cond_ptm_merge_df[f'peak_apex{cond_ptm}'])
    apex_condition = peak_apex_diff < apex_diff_cutoff

    #overlap condition
    overlap_condition = ~((cond_ptm_merge_df['peak_right'] < cond_ptm_merge_df[f'peak_left{cond_ptm}']) | (cond_ptm_merge_df['peak_left'] > cond_ptm_merge_df[f'peak_right{cond_ptm}']))

    #pass evaluation
    pass_condition = (apex_condition * overlap_condition).astype(int)
    return pass_condition
    
def replace_ptm_col_values(row, ptm, col):
    """Replace the values in the ptm column with the values in the col column.

    Args:
        row (pd.Series): The row of the DataFrame.
        ptm (str): The name of the ptm column.
        col (str): The name of the column to replace the ptm column values with.

    Returns:
        str: The value of the col column if the ptm column is 1, otherwise None.
    """
    if row[ptm] == 1:
        return str(row[col])
    else:
        return None

def ptm_matching(input_df):
    """Match the global peaks to the ptm peaks and summarize the results.
    In this version, all ptm peaks are tested for matching with all global peaks.
    This function allows for multiple ptm peaks to match a single global peak and vice versa.
    Additionally, each matched peak is defined by the global peak attributes (allows for rapid ptm matching).
    
    Args:
        input_df (pd.DataFrame): The DataFrame containing the peak data.
    
    Returns:
        pd.DataFrame: The DataFrame containing the matched peaks (global and ptm) and their attributes.
    """

    #create a skeleton dataframe to ensure that all protein replicates/conditions are present in the final dataframe
    skeleton_df = pd.DataFrame(columns = input_df.columns)
    skeleton_df[group_cols+['condition']] = input_df[group_cols+['condition']].drop_duplicates().reset_index(drop=True)

    #split the global and ptm peaks into separate dataframes
    global_df = input_df[input_df['ptm'] == 'global'].reset_index(drop=True)
    global_df['global'] = 1 

    if len(global_df) == 0:
        return None
    
    ptm_df = input_df[input_df['ptm'] != 'global'].reset_index(drop=True)

    #make sure that the all ptm columns are present for each protein replicate.
    for p in ptms:
        if p not in ptm_df['ptm'].unique() and p != 'global':
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=FutureWarning)
                ptm_df = pd.concat([ptm_df, skeleton_df.copy().assign(ptm=p)], ignore_index=True) #ignore this warning for now

    #iterate through each ptm df to merge with the global df
    ptm_df_list = [group for _, group in ptm_df.groupby('ptm')]

    for i in range(len(ptm_df_list)):
        #prep ptm dataframe
        ptm_df_i = ptm_df_list[i].reset_index(drop=True)
        ptm = ptm_df_i['ptm'].unique()[0]

        #merge global and ptm dataframes
        global_ptm_merge_i = global_df.merge(ptm_df_i, on=group_cols+['condition'], how='left', suffixes=('', '_ptm'))

        #get valid ptm matches - run evaluation function
        global_ptm_merge_i[ptm] = ptm_eval(global_ptm_merge_i, apex_diff_cutoff=threshold)

        if 'cluster' in group_cols:
            global_ptm_merge_i_summary = global_ptm_merge_i[group_cols+[ptm, 'condition', 'peak_apex', 'peak_id_ptm']]
            global_ptm_merge_i_summary = global_ptm_merge_i_summary.assign(peak_id_ptm = global_ptm_merge_i_summary.apply(replace_ptm_col_values, args=(ptm,'peak_id_ptm',), axis=1))
            global_ptm_merge_i_summary = global_ptm_merge_i_summary.groupby(group_cols+['peak_apex', 'condition'], as_index=False).agg({ptm:'sum', 'peak_id_ptm':lambda x: ';'.join(filter(None, x))})

            #merge ptm counts and clusters
            final_global_df = global_df.merge(global_ptm_merge_i_summary, on=group_cols+['peak_apex', 'condition'], how='left')
            final_global_df.drop(columns=['ptm'], inplace=True)

        else:
            #Summarize ptm matches - fill result df with values
            global_ptm_merge_i_summary = global_ptm_merge_i[group_cols+[ptm, 'condition', 'peak_apex','cluster_ptm', 'peak_id_ptm']]
            global_ptm_merge_i_summary = global_ptm_merge_i_summary.assign(cluster_ptm = global_ptm_merge_i_summary.apply(replace_ptm_col_values, args=(ptm,'cluster_ptm',), axis=1))
            global_ptm_merge_i_summary = global_ptm_merge_i_summary.assign(peak_id_ptm = global_ptm_merge_i_summary.apply(replace_ptm_col_values, args=(ptm,'peak_id_ptm',), axis=1))
            global_ptm_merge_i_summary = global_ptm_merge_i_summary.groupby(group_cols+['peak_apex', 'condition'], as_index=False).agg({ptm:'sum', 'cluster_ptm':lambda x: ';'.join(filter(None, x)), 'peak_id_ptm':lambda x: ';'.join(filter(None, x))})

            #merge ptm counts and clusters
            final_global_df = global_df.merge(global_ptm_merge_i_summary, on=group_cols+['peak_apex', 'condition'], how='left')

            #concatenated string of cluster_ptm to the cluster column
            final_global_df['cluster'] = final_global_df.apply(lambda x: ';'.join(filter(None, [x['cluster'], x['cluster_ptm']])), axis=1).astype(str)
            final_global_df.drop(columns=['cluster_ptm', 'ptm'], inplace=True)
    return final_global_df


###Condition peak matching functions###
def path_mean_dist(path):
    """Calculate the mean distance of the path from the mean of the path.
    Replace the NaNs with the threshold value.
    
    Args:
        path (np.ndarray): The path to calculate the mean distance of.
    
    Returns:
        np.ndarray: The squared mean distance of the path.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        path_dist = path - np.mean(path, axis=0, keepdims=True, where=~np.isnan(path)) #dtype=np.float64
    path_dist_wThreshold = np.nan_to_num(path_dist, nan=threshold)
    path_dist_sq = np.square(path_dist_wThreshold)
    return path_dist_sq

def path_score(path):
    """Calculate the score of the path based on the mean distance & variance of the path.
    
    Args:
        path (np.ndarray): The path to calculate the score of.
    
    Returns:
        float: The score of the path.
    """
    path_dist = path_mean_dist(path) 
    path_var = np.sum(path_dist) / len(path_dist)
    path_score = 1 / (path_var + 1)
    return path_score

def fill_values(index_combinations, fill_df, fill_column='peak_apex'):
    """Fill the values in the index_combinations array with the values from the test_df.
    
    Args:
        index_combinations (np.ndarray): The array of index combinations to fill.
        test_df (pd.DataFrame): The DataFrame containing the values to fill the index_combinations with.
        fill_column (str): The column to fill the index_combinations with (Default: 'peak_apex').
    
    Returns:
        np.ndarray: The filled index_combinations array with the new/replacement values."""
    #iterate through the index_combinations array
    index_combinations = index_combinations.copy().astype(object)
    for i in range(index_combinations.shape[0]):
        for j in range(index_combinations.shape[1]):

            #find the values corresponding to the index_combinations array from the fill_df's fill_column
            if pd.notna(index_combinations[i, j]):
                value = fill_df.loc[fill_df['index'] == index_combinations[i, j], fill_column].values
                if len(value) > 0:
                    index_combinations[i, j] = value[0]

    return index_combinations

def peak_alignment(peak_df): 
    """Align the peaks based on the peak apexes and return the aligned paths.
    Alignment is performed through the following steps:
    (1) Create a skeleton dataframe to ensure that all protein replicates/conditions are present in the final dataframe.
    (2) Create a list of index combinations to test the all possible peak alignments.
    (3) Score the paths and find the top scoring path.
    (4) Remove the peaks of the top scoring path from the peak_df and index combination iteration space.
    (5) Repeat steps 3 and 4 until all peak alignments are found.
    
    Args:
        peak_df (pd.DataFrame): The DataFrame containing the peak data.
    
    Returns:
        np.ndarray: The array of index paths containing the peak alignments.
    """

    #create a skeleton dataframe to ensure that all protein replicates/conditions are present in the final dataframe
    skeleton_df = pd.DataFrame(columns = peak_df.columns)
    skeleton_df[group_cols] = peak_df[group_cols].drop_duplicates().reset_index(drop=True)

    #ensures all conditions are matched
    for c in conditions:
        if c not in peak_df['condition'].unique():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=FutureWarning)
                peak_df = pd.concat([peak_df, skeleton_df.copy().assign(condition=c)], ignore_index=True) #edit to ignore this warning for now
    
    #create a list of indexes, grouped by condition for creating combinations and add NaN to the end of each list
    index_lists = peak_df.groupby('condition')['index'].apply(list).tolist()
    for p in index_lists:
        p.append(np.nan)

    #create all possible index combinations
    index_combinations = np.array(np.meshgrid(*index_lists)).T.reshape(-1, len(index_lists))
    peak_combinations = fill_values(index_combinations, peak_df, fill_column='peak_apex').astype(float)
    result_paths = []

    #iterate through the peak_combinations to find the top scoring path
    for i in range(0, len(peak_combinations)):
        path_scores = np.apply_along_axis(path_score, 1, peak_combinations) 
        top_combination_index = np.where(path_scores == path_scores.max())[0][0]
        top_combination_value = peak_combinations[top_combination_index]

        #if no more non-nan values stop iterating
        if np.isnan(top_combination_value).all():
            break
        else:
            #add the top scoring path to the result_paths and remove the path from the iteration space
            result_paths.append(index_combinations[top_combination_index])
            index_combinations = np.delete(index_combinations, np.where(index_combinations == index_combinations[top_combination_index])[0], axis=0)
            peak_combinations = np.delete(peak_combinations, np.where(peak_combinations == peak_combinations[top_combination_index])[0], axis=0)

    return np.array(result_paths)

def condition_matching(peak_df, group_columns): #
    """This function is for organizing the the matched peaks into a single dataframe.
    
    Args:
        peak_df (pd.DataFrame): The DataFrame containing the peak data.
        group_columns (list): The list of columns to group by.
    
    Returns:
        pd.DataFrame: The DataFrame containing the matched peaks and their attributes.
    """
    #perform peak alignment
    index_alignment = peak_alignment(peak_df) #

    #get non-group columns and create the output dataframe
    non_group_columns = peak_df.columns.difference(group_columns).to_list()
    output_df = pd.DataFrame({column:[] for column in group_columns + non_group_columns})

    #fill the output dataframe with the values from the peak_df using the index_alignment
    for column in group_columns:
        output_df[column] = [peak_df[column].iloc[0]] * len(index_alignment)

    for column in non_group_columns:
        output_df[column] = fill_values(index_alignment, peak_df, fill_column=column).tolist()

    return output_df


###Organize matching functions###
def matching_i(peak_table_i):
    """Perform the peak matching on the input peak table.
    This function is to be used with the multiprocessing module.
    
    Args:
        peak_table_i (pd.DataFrame): The DataFrame containing the peak data.
    
    Returns:
        pd.DataFrame: The DataFrame containing the matched peaks and their attributes.
    """

    input_df = peak_table_i.copy()
    ptm_mod_table = ptm_matching(input_df)
    
    if ptm_mod_table is None:
        return None
    else:
        return condition_matching(ptm_mod_table.reset_index(drop=True).reset_index(), group_cols)
    
#reorganize script to run with click
@click.command()
@click.option('--sql_db', '-s', required=True, help='SQLite database file for input and output')
@click.option('--cutoff', '-c', required=True, type=int, help='The maximum difference in peak apexes allowed for a match.')
@click.option('--peak_table_name', '-p', default='peak_table', help='The name of the table containing the peak data.')
@click.option('--align_table_name', '-a', default='alignment_table', help='The name of the table to save the alignment data.')
def main(sql_db, cutoff, peak_table_name, align_table_name):
    """This function is the main function for the peak matching and alignment script.
    It reads in the peak table from the SQLite database, performs the peak matching and alignment, and saves the results back to the database.
    """
    t0 = time.time()
    #import the peak_table and define global variables
    click.echo('Importing peak table and defining global variables...')
    manager = SQLitePeakMatchingManager(sql_db, peak_table_name)
    match_df_list = manager.match_df_list.copy()

    global peak_table, match_id, group_cols, conditions, ptms, threshold
    peak_table, match_id, group_cols, conditions, ptms, threshold = manager.peak_table.copy(), manager.match_id.copy(), manager.group_cols.copy(), manager.conditions.copy(), manager.ptms.copy(), cutoff
    manager.close_connection()

    #perform peak matching and alignment
    click.echo('Performing peak matching and alignment...')
    mapped_result = map(matching_i, match_df_list)
    clustered_global_intensity_table = pd.concat(mapped_result, ignore_index=True).reset_index(drop=True)

    #move all columns with 'peak' to the end using regex, and drop a few columns
    click.echo('Reorganizing columns...')
    clustered_global_intensity_table = clustered_global_intensity_table[clustered_global_intensity_table.columns.drop(list(clustered_global_intensity_table.filter(regex='peak'))).tolist() + clustered_global_intensity_table.filter(regex='peak').columns.tolist()]
    clustered_global_intensity_table = clustered_global_intensity_table.drop(columns=['index', 'match_id'], axis=1).reset_index(drop=True)
    
    #save results to SQLite database
    click.echo('Saving results to SQLite database...')
    manager = SQLitePeakMatchingManager(sql_db, peak_table_name)
    manager.save_to_SQL(clustered_global_intensity_table, align_table_name)
    manager.close_connection()

    #print runtime in minutes and seconds
    t1 = time.time()
    click.echo(f'Peak matching and alignment completed in {(t1-t0)/60:.2f} minutes.')


if __name__ == '__main__':
    main()
