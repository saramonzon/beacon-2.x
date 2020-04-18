"""
Samples/Individuals Endpoint.

* ``/samples`` 
* ``/individuals`` 

Query samples/individuals with specific parameters and/or containing a certain variant. 

.. note:: See ``schemas/samples.json`` for checking the parameters accepted in this endpoint.
"""
import ast
import logging
import requests
import random
import pandas as pd

from .exceptions import BeaconBadRequest, BeaconServerError, BeaconForbidden, BeaconUnauthorised
from .. import __apiVersion__, __id__
from ..conf.config import DB_SCHEMA

from ..utils.polyvalent_functions import create_prepstmt_variables, filter_exists, datasetHandover
from ..utils.polyvalent_functions import prepare_filter_parameter, parse_filters_request
from ..utils.polyvalent_functions import fetch_datasets_access, access_resolution
from ..utils.models import variant_object, variantAnnotation_object, biosample_object, individual_object

from .genomic_query import fetch_resulting_datasets, fetch_variantAnnotations, snp_resultsHandover


LOG = logging.getLogger(__name__)


# ----------------------------------------------------------------------------------------------------------------------
#                                         FORMATTING
# ----------------------------------------------------------------------------------------------------------------------

def transform_record(response):
    
    response["datasetId"] = response.pop("stable_id_dt")
    response["internalId"] = response.pop("dataset_id")
    response["exists"] = True
    response["variantCount"] = response.pop("variant_cnt")  
    response["callCount"] = response.pop("call_cnt") 
    response["sampleCount"] = response.pop("sample_cnt") 
    response["frequency"] = 0 if response.get("frequency") is None else float(response.pop("frequency"))
    response["numVariants"] = 0 if response.get("num_variants") is None else response.pop("num_variants")
    response["info"] = {"accessType": response.pop("access_type"),
                        "matchingSampleCount": 0 if response.get("matching_sample_cnt") is None else response.pop("matching_sample_cnt")}
    response["datasetHandover"] = datasetHandover(response["datasetId"])
    
    return response


def create_query(processed_request):
    """
    Restructure the request to build the query object
    """
    query = {
        "variant": {
            "referenceBases": "" if not processed_request.get("referenceBases") else processed_request.get("referenceBases"),
            "alternateBases": "" if not processed_request.get("alternateBases") else processed_request.get("alternateBases"),
            "referenceName": "" if not processed_request.get("referenceName") else processed_request.get("referenceName"),
            "start": None if not processed_request.get("start") else processed_request.get("start"),
            "end": None if not processed_request.get("end") else processed_request.get("end"),
            "assemblyId": "" if not processed_request.get("assemblyId") else processed_request.get("assemblyId")
            },
        "datasets": {
            "datasetIds": None if not processed_request.get("datasetIds") else processed_request.get("datasetIds"),
            "includeDatasetResponses": "ALL" if not processed_request.get("includeDatasetResponses") else processed_request.get("includeDatasetResponses")
             },
    "filters": None if not processed_request.get("filters") else processed_request.get("filters"),
    "customFilters": None if not processed_request.get("customFilters") else processed_request.get("customFilters"),
    }

    return query


# ----------------------------------------------------------------------------------------------------------------------
#                                         MAIN FUNCTIONS
# ----------------------------------------------------------------------------------------------------------------------

# Definition of interesting columns depending on the target object, use for create_individuals_object() and 
# create_samples_object()
# sample_columns = ['sample_id', 'sample_stable_id', 'tissue', 'description']
# individual_columns = ['patient_id','patient_stable_id', 'sex', 'age_of_onset', 'disease']
# variant_columns = ['unique_id', 'dataset_id', 'data_id','chromosome', 'variant_id', 'reference', 
#                    'alternate', 'start', 'end', 'type', 'sv_length', 'variant_cnt', 'call_cnt', 
#                    'sample_cnt', 'matching_sample_cnt', 'frequency']
# Definition of interesting columns depending on the target object
sample_columns = ['sample_id', 'sample_stable_id', 'description', 'biosample_status', 'individual_age_at_collection_age',
                 'individual_age_at_collection_age_group', 'organ', 'tissue', 'cell_type', 'obtention_procedure', 
                 'tumor_progression', 'tumor_grade', 'patient_stable_id']
individual_columns = ['patient_id','patient_stable_id', 'sex', 'ethnicity', 'geographic_origin']
variant_columns = ['unique_id','chromosome', 'variant_id', 'reference', 
                   'alternate', 'start', 'end', 'type']
dataset_columns = ['dataset_id', 'stable_id_dt', 'variant_cnt', 'call_cnt', 
                   'sample_cnt', 'matching_sample_cnt', 'frequency', 'access_type']
variant_and_dataset_columns = variant_columns + dataset_columns
disease_columns = ['disease', 'age', 'age_group', 'stage', 'family_history']
pedigree_columns = ['pedigree_id', 'pedigree_role', 'number_of_individuals_tested', 'pedigree_disease', 'pedigree_description'] 


async def create_variantsFound_object(db_pool, variants_df, include_dataset, processed_request, valid_datasets):
    """
    Create the raw object and shape as the spec at the same time by calling other functions.
    """
    # Decide to show all variants or just a subset
    include_all_variants = "false" if not processed_request.get("includeAllVariants") else processed_request.get("includeAllVariants")
    reduced_response = True if include_all_variants == "false" else False
    all_variants = list(variants_df.unique_id)
    if reduced_response and len(all_variants) > 5:
        all_variants = list(variants_df.unique_id)
        subset_variants = random.sample(all_variants, 5)
        variants_df = variants_df[variants_df.unique_id.isin(subset_variants)]

    by_variant = variants_df.groupby('unique_id')

    # Iterate and create the objects
    variants_found_object = []
    for variant_id, df in by_variant:
        # gather the raw info
        variant_info_raw = df[variant_columns].drop_duplicates().to_dict('r')[0]
        datasetAlleleResponses_raw = df[dataset_columns].drop_duplicates().to_dict('r')
        datasetAlleleResponses = [transform_record(record) for record in datasetAlleleResponses_raw]
        
        # rename variant_info keys
        variant_info = {
            "variantId": variant_info_raw.get("variant_id"),
            "chromosome":  variant_info_raw.get("chromosome"),
            "referenceBases": variant_info_raw.get("reference"),
            "alternateBases": variant_info_raw.get("alternate"),
            "variantType": variant_info_raw.get("variant_type"),
            "start": variant_info_raw.get("start"), 
            "end": variant_info_raw.get("end")
        }
        
        # shape datasetAlleleResponse
        
        # If  the includeDatasets option is ALL or MISS we have to "create" the miss datasets (which will be tranformed also) and join them to the datasetAlleleResponses
        if include_dataset in ['ALL', 'MISS']:

            list_hits = [record["internalId"] for record in datasetAlleleResponses]
            list_all = valid_datasets
            # list_all = await get_datasetspersample(db_pool, sample_id)
            list_all_valid = [x for x in list_all if x in valid_datasets]
            accessible_missing = [int(x) for x in list_all_valid if x not in list_hits]
            miss_datasets = await fetch_resulting_datasets(db_pool, "", misses=True, accessible_missing=accessible_missing)
            datasetAlleleResponses += miss_datasets
        datasetAlleleResponses = filter_exists(include_dataset, datasetAlleleResponses)
        
        
        # do some extra stuff for variantAnnotations
        rsID, cellBase_dict, dbSNP_dict = await fetch_variantAnnotations(variant_info)
        if rsID: variant_info["variantId"] = rsID
        
        # create the dict
        variant_found = {
            'variant': variant_object(processed_request, variant_info),
            'datasetAlleleResponses': datasetAlleleResponses,
            "variantAnnotations": variantAnnotation_object(processed_request, cellBase_dict, dbSNP_dict, {}),
            "variantHandover": snp_resultsHandover(rsID) if rsID else '',
            "info": {}
        }
        
        variants_found_object.append(variant_found)
    return variants_found_object



async def create_individuals_object(db_pool, main_df, include_dataset, processed_request, valid_datasets, simple = False):
    by_individual = main_df.groupby('patient_id')

    individual_responses_list = []

    # iterating through the samples and creating the objects
    for individual_id, individual_df in by_individual:
        individual_response = {}

        individual = individual_df[individual_columns].drop_duplicates().to_dict('r')[0]

        # adding the info about diseases and pedigrees
        diseases = individual_df[disease_columns].drop_duplicates().to_dict('r')
        individual.update({'diseases': diseases})
        pedigrees = individual_df[pedigree_columns].drop_duplicates().to_dict('r')
        individual.update({'pedigrees': pedigrees})

        individual_response['individual'] = individual_object(individual, processed_request)

        if not simple:
            samples_object = individual_df[sample_columns].drop_duplicates().to_dict('r')
            individual_response['samples'] = [biosample_object(sample, processed_request)
                            for sample in samples_object]

            # variants_raw = individual_df[variant_columns].drop_duplicates().to_dict('r')
            # individual_response['variantsFound'] = variants_raw
            
            variants_df = individual_df[variant_and_dataset_columns].drop_duplicates()
            individual_response['variantsFound'] = await create_variantsFound_object(db_pool, variants_df, include_dataset, processed_request, valid_datasets)
            
            individual_responses_list.append(individual_response)
        else:
            individual_responses_list.append(individual_response['individual'])

    return individual_responses_list


async def create_samples_object(db_pool, main_df, include_dataset, processed_request, valid_datasets, simple = False):
    by_sample = main_df.groupby('sample_id')

    sample_responses_list = []

    # iterating through the samples and creating the objects
    for sample_id, sample_df in by_sample:
        sample_response = {}


        sample_object = sample_df[sample_columns].drop_duplicates().to_dict('r')[0]
        sample_response['sample'] = biosample_object(sample_object, processed_request)

        
        if not simple:
            # individuals_object = sample_df[individual_columns].drop_duplicates().to_dict('r')
            individual_response_list = []
            by_individual = sample_df.groupby('patient_id')

            for individual_id, individual_df in by_individual:

                individual = individual_df[individual_columns].drop_duplicates().to_dict('r')[0]

                # adding the info about diseases and pedigrees
                diseases = individual_df[disease_columns].drop_duplicates().to_dict('r')
                individual.update({'diseases': diseases})
                pedigrees = individual_df[pedigree_columns].drop_duplicates().to_dict('r')
                individual.update({'pedigrees': pedigrees})

                individual_response_list.append(individual_object(individual, processed_request))

            sample_response['individuals'] = individual_response_list

            # variants_raw = sample_df[variant_columns].drop_duplicates().to_dict('r')
            # sample_response['variantsFound'] = 'variants_raw'

            variants_df = sample_df[variant_and_dataset_columns].drop_duplicates()
            sample_response['variantsFound'] = await create_variantsFound_object(db_pool, variants_df, include_dataset, processed_request, valid_datasets)

            sample_responses_list.append(sample_response)
        else:
            sample_responses_list.append(sample_response['sample'])
        
    return sample_responses_list


async def get_results(db_pool, filters_dict, valid_datasets, processed_request, request, include_dataset): 
    """
    Fetches all the data performing a complex query to the DB where all the info about
    samples, variants and individuals can be queried at once. 
    Returns a results object with the 'raw' keys and values. This will have to be shaped
    to match the spec later. 
    """

    # Gathering the variant related parameters passed in the request
    chromosome = '' if not processed_request.get("referenceName") else processed_request.get("referenceName") 
    reference = '' if not processed_request.get("referenceBases") else processed_request.get("referenceBases") 
    alternate = '' if not processed_request.get("alternateBases") else processed_request.get("alternateBases")
    start = 'null' if not processed_request.get("start") else processed_request.get("start")
    end = 'null' if not processed_request.get("end") else processed_request.get("end")
    reference_genome = '' if not processed_request.get("assemblyId") else processed_request.get("assemblyId")

    dataset_ids = ",".join([str(i) for i in valid_datasets])

    # Preparing the SQL query with the clauses regarding individuals and samples based on the requests
    sentence = []
    target_table_sample = f"{DB_SCHEMA}.beacon_sample_table"
    target_table_patient = f"{DB_SCHEMA}.patient_table"
    samples_filter_dict = None if not filters_dict.get(target_table_sample) else filters_dict.get(target_table_sample)
    patients_filter_dict = None if not filters_dict.get(target_table_patient) else filters_dict.get(target_table_patient)

    # Update the patients_filter_dict with disease and pedigree info
    target_table_patient_disease = f"{DB_SCHEMA}.patient_disease_table"
    if filters_dict.get(target_table_patient_disease):
        patients_filter_dict.update(filters_dict.get(target_table_patient_disease))

    target_table_patient_pedigree = f"{DB_SCHEMA}.patient_pedigree_table"
    if filters_dict.get(target_table_patient_pedigree):
        patients_filter_dict.update(filters_dict.get(target_table_patient_pedigree))

    # Join everything in an SQL-friendly format
    if samples_filter_dict:
        for column, list_values in samples_filter_dict.items():
            sentence_part = f""" s.{column} IN ('{"','".join(list_values)}')"""
            sentence.append(sentence_part)
    if patients_filter_dict:
        for column, list_values in patients_filter_dict.items():
            sentence_part = f""" p.{column} IN ('{"','".join(list_values)}')"""
            sentence.append(sentence_part)
    
    sentence = " AND ".join(sentence)
    sentence_exists = False if not sentence else True
    sentence = 'null' if not sentence_exists else sentence

    # Fetching the info 
    async with db_pool.acquire(timeout=180) as connection:
        try:
            query  = f"""SELECT concat_ws(':', data_t.chromosome, data_t.variant_id, data_t.reference, data_t.alternate, data_t.start, data_t.end, data_t.type) AS unique_id,
                            data_t.dataset_id, d_t.reference_genome, d_t.stable_id as stable_id_dt, d_t.access_type, vsp_t.data_id, data_t.chromosome, data_t.variant_id, 
                            data_t.reference, data_t.alternate, data_t.start, data_t.end, data_t.type, data_t.sv_length, data_t.variant_cnt, data_t.call_cnt, data_t.sample_cnt, 
                            data_t.matching_sample_cnt, data_t.frequency, vsp_t.sample_id, vsp_t.sample_stable_id, vsp_t.description, vsp_t.biosample_status, 
							vsp_t.individual_age_at_collection_age, vsp_t.individual_age_at_collection_age_group, vsp_t.organ, vsp_t.tissue, vsp_t.cell_type, 
							vsp_t.obtention_procedure, vsp_t.tumor_progression, vsp_t.tumor_grade,
							vsp_t.patient_id, vsp_t.patient_stable_id, 
                            vsp_t.sex, vsp_t.ethnicity, vsp_t.geographic_origin,
							-- patient_disease_table
							vsp_t.disease, vsp_t.age, vsp_t.age_group, vsp_t.stage, vsp_t.family_history,
							-- patient_pedigree_table
							vsp_t.pedigree_id, vsp_t.pedigree_role, vsp_t.number_of_individuals_tested, vsp_t.pedigree_disease, vsp_t.pedigree_description
                            FROM public.beacon_data_table as data_t
                            join (select * 
                                    from (SELECT s.id as s_id, s.stable_id as sample_stable_id, s.description, 
										  	s.biosample_status, s.individual_age_at_collection_age, s.individual_age_at_collection_age_group, 
										  	s.organ, s.tissue, s.cell_type, s.obtention_procedure, s.tumor_progression, s.tumor_grade,
                                            p.*
                                            FROM beacon_sample_table s 
                                            JOIN (SELECT p.id as patient_id, p.stable_id as patient_stable_id, p.sex, p.ethnicity, 
													p.geographic_origin,
													-- patient_disease_table
													pd.disease, pd.age, pd.age_group, pd.stage, pd.family_history,
													-- patient_pedigree_table
													pp.pedigree_id, pp.pedigree_role, pp.number_of_individuals_tested, pp.pedigree_disease, pp.pedigree_description
															FROM patient_table p 
															-- patient_disease_table
															LEFT JOIN patient_disease_table as pd
															ON p.id = pd.patient_id
															-- patient_pedigree_table
															LEFT JOIN (SELECT patient_id, pedigree_id, pedigree_role, number_of_individuals_tested, 
																	   disease as pedigree_disease, pedigree_table.description as pedigree_description
																		FROM patient_pedigree_table
																		JOIN pedigree_table
																		ON pedigree_id = id) as pp
															ON p.id = pp.patient_id) as p 
										  	ON s.patient_id = p.patient_id
                                            -- patient and sample filters
                                            WHERE (CASE WHEN {sentence_exists} THEN {sentence} ELSE true END) AND s.tissue IS NOT NULL) as sample_t
                                    join public.beacon_data_sample_table as data_sample_t
                                    on sample_t.s_id = data_sample_t.sample_id) as vsp_t
                            on data_t.id = vsp_t.data_id
                            join public.beacon_dataset_table as d_t
                            on data_t.dataset_id = d_t.id
                            WHERE 
                            -- dataset filter
                            data_t.dataset_id IN ({dataset_ids}) 
                            -- variant filter
                            AND (CASE
                                WHEN nullif('{chromosome}', '') IS NOT NULL THEN chromosome = '{chromosome}' ELSE true
                                END)
                            AND (CASE
                                WHEN nullif('{reference}', '') IS NOT NULL THEN reference = '{reference}' ELSE true
                                END)
                            AND (CASE
                                WHEN nullif('{alternate}', '') IS NOT NULL THEN alternate = '{alternate}' ELSE true
                                END)
                            AND (CASE
                                WHEN {start} IS NOT NULL THEN start = {start} ELSE true
                                END)
                            AND (CASE
                                WHEN {end} IS NOT NULL THEN 'end' = {end} ELSE true
                                END)
                            AND (CASE
                                WHEN nullif('{reference_genome}', '') IS NOT NULL THEN reference_genome = '{reference_genome}' ELSE true
                                END);"""

            LOG.debug(f"QUERY samples/individuals: {query}")
            statement = await connection.prepare(query)
            db_response = await statement.fetch()

            response = []
            for record in list(db_response):
                response.append(dict(record))
        except Exception as e:
            raise BeaconServerError(f'Query samples/individuals DB error: {e}')
        
        endpoint = request.path
        if response: 
            # Converting the response to a DataFrame 
            response_df = pd.DataFrame(response)
            # Making sure we don't have NaN values
            response_df = response_df.where(response_df.notnull(), None)

            # Calling the functions to create the objects
            # Depending on the endpoint, the function changes
            LOG.debug(f"Arranging the response for the {endpoint} endpoint.")
            if endpoint == '/individuals':
                response_arranged = await create_individuals_object(db_pool, response_df, include_dataset, processed_request, valid_datasets)
            else:
                response_arranged = await create_samples_object(db_pool, response_df, include_dataset, processed_request, valid_datasets)
            LOG.debug(f"Arrangement done for the {endpoint} endpoint.")
            # Returning the arrange response
            return response_arranged
        else:
            LOG.debug(f"No response for this query on the {endpoint} endpoint.")
            return []


async def get_results_simple(db_pool, valid_datasets, request, processed_request, target_id_req = ''):
    """
    Fetches the samples or individuals info ONLY if the query doesn't specify any parameters. 
    """
    dataset_ids = ",".join([str(i) for i in valid_datasets])

    query_samples = f"""SELECT s.id as sample_id, s.stable_id as sample_stable_id, 
                        s.description, s.biosample_status, s.individual_age_at_collection_age, 
                        s.individual_age_at_collection_age_group, s.organ, s.tissue, s.cell_type, 
                        s.obtention_procedure, s.tumor_progression, s.tumor_grade, s.patient_id,
                        p.stable_id as patient_stable_id, dts.dataset_id 
                        FROM beacon_sample_table as s
                        JOIN beacon_dataset_sample_table as dts
                        ON s.id = dts.sample_id
                        JOIN patient_table as p
                        ON s.patient_id = p.id
                        WHERE 
                        -- dataset filter
                        dataset_id IN ({dataset_ids})
                        -- sample id filter
                        AND (CASE
                            WHEN nullif('{target_id_req}', '') IS NOT NULL THEN s.stable_id = '{target_id_req}' ELSE true
                            END);"""

    query_individuals = f"""SELECT p.id as patient_id, p.stable_id as patient_stable_id, p.sex, p.ethnicity, 
                            p.geographic_origin, dataset_id,
                            -- patient_disease_table
                            pd.disease, pd.age, pd.age_group, pd.stage, pd.family_history,
                            -- patient_pedigree_table
                            pp.pedigree_id, pp.pedigree_role, pp.number_of_individuals_tested, pp.pedigree_disease, pp.pedigree_description
                                    FROM (SELECT p.*, s.id as sample_id FROM patient_table as p 
                                    JOIN beacon_sample_table as s
                                    ON p.id = s.patient_id) as p
                                    JOIN beacon_dataset_sample_table as dts
                                    ON p.sample_id = dts.sample_id
                                    -- patient_disease_table
                                    LEFT JOIN patient_disease_table as pd
                                    ON p.id = pd.patient_id
                                    -- patient_pedigree_table
                                    LEFT JOIN (SELECT patient_id, pedigree_id, pedigree_role, number_of_individuals_tested, 
											   disease as pedigree_disease, pedigree_table.description as pedigree_description
												FROM patient_pedigree_table
												JOIN pedigree_table
												ON pedigree_id = id) as pp
						            ON p.id = pp.patient_id
                                    WHERE 
                                    -- dataset filter
                                    dataset_id IN ({dataset_ids})
                                    -- individual id filter
                                    AND (CASE
                                        WHEN nullif('{target_id_req}', '') IS NOT NULL THEN p.stable_id = '{target_id_req}' ELSE true
                                        END);"""

    # performing the actual query to the DB
    async with db_pool.acquire(timeout=180) as connection:
        try:
            endpoint = request.path
            if endpoint.startswith('/samples'):
                query = query_samples
            elif endpoint.startswith('/individuals'):
                query = query_individuals
            else:
                LOG.debug("The endpoint is different than 'samples' and 'individuals'. Please try again.")
                raise BeaconServerError(f'Query simple samples/individuals error')


            LOG.debug(f"QUERY simple samples/individuals: {query}")
            statement = await connection.prepare(query)
            db_response = await statement.fetch()

            response = []
            for record in list(db_response):
                response.append(dict(record))
        except Exception as e:
            raise BeaconServerError(f'Query simple samples/individuals DB error: {e}')
    # parsing the response
    if response: 
        # Converting the response to a DataFrame 
        response_df = pd.DataFrame(response)
        # Making sure we don't have NaN values
        response_df = response_df.where(response_df.notnull(), None)

        # Calling the functions to create the objects
        # Depending on the endpoint, the function changes
        LOG.debug(f"Arranging the response for the {endpoint} endpoint.")
        if endpoint.startswith('/individuals'):
            response_arranged = await create_individuals_object('', response_df, '', processed_request, '', simple = True)
        else:
            response_arranged = await create_samples_object('', response_df, '', processed_request, '', simple = True)
        LOG.debug(f"Arrangement done for the {endpoint} endpoint.")
        # Returning the arrange response
        return response_arranged
    else:
        LOG.debug(f"No response for this query on the {endpoint} endpoint.")
        return []



async def get_valid_datasets(db_pool, dataset_filters):
    """
    Returns a list of the dataset ids that pass the filters
    """
    async with db_pool.acquire(timeout=180) as connection:
        id_list = []
        try: 
            if dataset_filters and dataset_filters != 'null':
                query  = f"""SELECT id
                            FROM beacon_dataset_table
                            WHERE {dataset_filters};"""
            else:
                query  = f"""SELECT id
                            FROM beacon_dataset_table;"""


            LOG.debug(f"QUERY valid datasets: {query}")
            statement = await connection.prepare(query)
            db_response = await statement.fetch()

            for record in list(db_response):
                id_list.append(dict(record).get("id"))

        except Exception as e:
            raise BeaconServerError(f'Query filtered datasets DB error: {e}')

    return id_list


# ----------------------------------------------------------------------------------------------------------------------
#                                         HANDLER FUNCTION
# ----------------------------------------------------------------------------------------------------------------------

async def sample_ind_request_handler(db_pool, processed_request, request):
    """
    Execute query with SQL function.
    """

    # First we are going to get the lists of the available datasets
    public_datasets, registered_datasets, controlled_datasets = await fetch_datasets_access(db_pool, str(processed_request.get("datasetIds")))

        ##### TEST
        # access_type, accessible_datasets = access_resolution(request, request['token'], request.host, public_datasets, registered_datasets, controlled_datasets)
        # LOG.info(f"The user has this types of access: {access_type}")
        # query_parameters[-2] = ",".join([str(id) for id in accessible_datasets])
        ##### END TEST

    # NOTICE that right now we will just focus on the PUBLIC ones to ease the process, so we get all their ids and add them to the query
    available_datasets = public_datasets

    # We will output the datasets depending on the includeDatasetResponses parameter
    include_dataset = ""
    if processed_request.get("includeDatasetResponses"):
        include_dataset  = processed_request.get("includeDatasetResponses")
    else:
        include_dataset  = "ALL"

    # Then we are going to parse the filters to separate them depending on their target table
    filters_list = [] if not processed_request.get("filters") else processed_request.get("filters")
    custom_filters_list = [] if not processed_request.get("customFilters") else processed_request.get("customFilters")
    if filters_list or custom_filters_list:
        all_filters_list = filters_list + custom_filters_list
        dataset_filters, filters_dict = await prepare_filter_parameter(db_pool, all_filters_list)
    else:
        dataset_filters = ""
        filters_dict = {}
        

    # we'll need to apply the dataset related filters (if there is any) so we are going to generate a list
    # with the ones that pass the dataset_filters filters
    valid_datasets = await  get_valid_datasets(db_pool, dataset_filters)

    # The intersection between the datasets that are available by access and the datasets that have passed the filters
    # is the final list of valid_datasets
    valid_datasets = [dataset for dataset in valid_datasets if dataset in available_datasets]

    # Now we perform the main query to the DB to retrieve all the sample, individual and variant raw data
    # if we don't have parameters, that means we can respond with a simple list of samples/individuals
    if not processed_request or (len(processed_request) == 1 and ('individual' in processed_request.keys() or 'biosample' in processed_request.keys())):
        results = await get_results_simple(db_pool, valid_datasets, request, processed_request)
    else:
        results = await get_results(db_pool, filters_dict, valid_datasets, processed_request, request, include_dataset)

            
    # In the response only a subset of variants will be shown, except when includeAllVariants is set to true
    # if that's not the case, we are going to create an object to facilitate the link for getting all of them
    all_variants_url = { "info": "For optimization reasons, only a subset of variants are shown for each sample. If you would like to get all of them, visit the link below.",
                        "url": f"https://testv2-beacon-api.ega-archive.org{request.rel_url}&includeAllVariants=true"
                        }

    # We need to restructure the query to create the object that will be shown
    query = create_query(processed_request)

    # Make lists of the models requests to show it in the response
    variant = processed_request.get("variant").split(",") if processed_request.get("variant") else []
    variantAnnotation = processed_request.get("variantAnnotation").split(",") if processed_request.get("variantAnnotation") else [] 
    variantMetadata = processed_request.get("variantMetadata").split(",") if processed_request.get("variantMetadata") else [] 
    biosample = processed_request.get("biosample").split(",") if processed_request.get("biosample") else [] 
    individual = processed_request.get("individual").split(",") if processed_request.get("individual") else [] 

    # Once all this is done, we build the response object
    beacon_response = {
                    "meta": {
                        "Variant": ["beacon-variant-v0.1", "ga4gh-variant-representation-v0.1"],
  	                    "VariantAnnotation": ["beacon-variant-annotation-v1.0"],
                        "VariantMetadata": ["beacon-variant-metadata-v1.0"],
                        "biosample": ["beacon-biosample-v0.1", "ga4gh-phenopacket-biosample-v0.1"],
                        "individual": ["beacon-individual-v0.1", "ga4gh-phenopacket-individual-v0.1"],
                    },
                    "value": { 'beaconId': __id__,
                        'apiVersion': __apiVersion__,
                        'exists': any(results),
                        # 'exists': any([dataset['exists'] for result in results for variant in result["variantsFound"] for dataset in variant["datasetAlleleResponses"]]),
                        'request': { "meta": { "request": { 
                                                            "Variant": ["beacon-variant-v0.1"]  + variant,
                                                            "VariantAnnotation": ["beacon-variant-annotation-v1.0"] + variantAnnotation,
                                                            "VariantMetadata": ["beacon-variant-metadata-v1.0"] + variantMetadata,
                                                            "sample": ["beacon-biosample-v0.1"] + biosample,
                                                            "individual": ["beacon-individual-v0.1"] + individual
                                                        },
                                                "apiVersion": __apiVersion__,
                                            },
                                    "query": query
                                    },
                        'results': results,
                        'info': None,
                        'resultsHandover': None if processed_request.get("includeAllVariants") == "true" else all_variants_url,
                        'beaconHandover': [ { "handoverType" : {
                                                "id" : "CUSTOM",
                                                "label" : "Organization contact"
                                                },
                                                "note" : "Organization contact details maintaining this Beacon",
                                                "url" : "mailto:beacon.ega@crg.eu"
                                            } ]
                        
                        }
                    }
    return beacon_response