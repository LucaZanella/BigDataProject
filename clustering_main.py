'''

  ________    ______   ______     _          ____        __
 /_  __/ /   / ____/  /_  __/____(_)___     / __ \____ _/ /_____ _
  / / / /   / /        / / / ___/ / __ \   / / / / __ `/ __/ __ `/
 / / / /___/ /___     / / / /  / / /_/ /  / /_/ / /_/ / /_/ /_/ /
/_/ /_____/\____/    /_/ /_/  /_/ .___/  /_____/\__,_/\__/\__,_/
                               /_/


Authors: Willi Menapace <willi.menapace@studenti.unitn.it>
         Luca Zanella <luca.zanella-3@studenti.unitn.it>
         Daniele Giuliani <daniele.giuliani@studenti.unitn.it>

Dataset clustering
You may want to skip the k elbow error calculation, the best k=5 is already given
By using the given clustering_model.model file you may skip the whole clustering phase to produce just
the clustered dataset

IMPORTANT: Please also ensure that Spark driver memory is set in your spark configuration files
           to a sufficient amount (>= 2g), otherwise you may experience spark running out of memory while writing
           parquet results

Required files: Clean dataset

Parameters to set:
master -> The url for the spark cluster, set to local for your convenience
dataset_folder -> Location of the dataset
results_folder -> Location where to save results
'''

import pyspark
from pyspark.ml import Pipeline
from pyspark.ml import PipelineModel
from pyspark.sql import SparkSession
import pyspark.sql.functions
from pyspark.sql.types import *
from pyspark.sql.functions import *

from pyspark.ml.clustering import KMeans
from pyspark.ml.feature import OneHotEncoder, StringIndexer, VectorAssembler, StandardScaler, OneHotEncoderEstimator, SQLTransformer

import schema_conversion
from schema import *
from computed_columns import *
from statistics import *

taxi_company_indexed_property = 'taxi_company_indexed'
pickup_hour_property = "pickup_hour"
dropoff_hour_property = "dropoff_hour"
weekend_property = 'weekend'
speed_property = 'speed'
taxi_company_encoded_property = taxi_company_indexed_property + '_encoded'
ratecode_id_encoded_property = ratecode_id_property + '_encoded'
payment_type_encoded_property = payment_type_property + '_encoded'
unscaled_vector_property = "unscaled_features_vector"
scaled_vector_property = "scaled_features_vector"
partial_clustering_features_property = 'partial_features'
clustering_features_property = 'clustering_features'

def cluster(dataset, spark, max_clusters = 5, max_iterations = 40, clustering_prediction_property = "clustering_predictions"):
    '''
    Performs clustering on the given dataset
    '''

    taxi_company_indexer = StringIndexer(inputCol=taxi_company_property, outputCol=taxi_company_indexed_property)

    pickup_hour_extractor = SQLTransformer(statement = "SELECT *, HOUR(" + pickup_datetime_property + ") AS " + pickup_hour_property + " FROM __THIS__")
    dropoff_hour_extractor = SQLTransformer(statement = "SELECT *, HOUR(" + dropoff_datetime_property + ") AS " + dropoff_hour_property + " FROM __THIS__")
    weekend_extractor = SQLTransformer(statement = "SELECT *, (DAYOFWEEK(" + pickup_datetime_property + ") == 6 OR DAYOFWEEK(" + pickup_datetime_property + ") == 5 OR DAYOFWEEK(" + pickup_datetime_property + ") == 7) AS " + weekend_property + " FROM __THIS__")

    speed_extractor = SQLTransformer(statement = "SELECT *, (" + trip_distance_property + " / (timestamp_diff(" + dropoff_datetime_property + ", " + pickup_datetime_property + ") / 1000)) AS " + speed_property + " FROM __THIS__")

    one_hot_encoder = OneHotEncoderEstimator(inputCols=[taxi_company_indexed_property, ratecode_id_property, payment_type_property], outputCols=[taxi_company_encoded_property, ratecode_id_encoded_property, payment_type_encoded_property], handleInvalid='keep')
    vector_assembler = VectorAssembler(inputCols=[taxi_company_indexed_property, ratecode_id_encoded_property, payment_type_encoded_property, weekend_property], outputCol=partial_clustering_features_property)

    unscaled_vector_assembler = VectorAssembler(inputCols=[passenger_count_property, trip_distance_property, fare_amount_property, tolls_amount_property, pickup_hour_property, dropoff_hour_property, speed_property], outputCol=unscaled_vector_property)
    scaler = StandardScaler(inputCol=unscaled_vector_property, outputCol=scaled_vector_property, withStd=True, withMean=True)

    complete_vector_assembler = VectorAssembler(inputCols=[partial_clustering_features_property, scaled_vector_property], outputCol=clustering_features_property)

    kmeans = KMeans(featuresCol=clustering_features_property, predictionCol=clustering_prediction_property, k=max_clusters, maxIter=max_iterations)

    pipeline = Pipeline(stages=[pickup_hour_extractor, dropoff_hour_extractor, weekend_extractor, speed_extractor, taxi_company_indexer, one_hot_encoder, unscaled_vector_assembler, scaler, vector_assembler, complete_vector_assembler, kmeans])

    model = pipeline.fit(dataset)

    return model

def compute_k_elbow(dataset, spark, save_results_folder, k_from=3, k_to=10, step_size=2, training_fraction=0.001, evaluation_fraction=0.1):
    '''
    Computes the clustering error curve as a function of k
    Saves obtained errors in text files

    Training fraction and evaluation fraction control the portion of the dataset used for clustering and evaluation
    of clustering error. You may want to regulate that according to your available computing power
    '''

    k_results = {}

    for k in range(k_from, k_to, step_size):

        print("Setting k=" + str(k))

        training_dataset = dataset.sample(training_fraction)

        print("Building clustering Model")
        clustering_model = cluster(training_dataset, spark, max_clusters=k)
        print("Computing model cost")

        featured_dataset = dataset
        kmeans_stage = 10
        for i in range(kmeans_stage):
            featured_dataset = clustering_model.stages[i].transform(featured_dataset)

        current_result = clustering_model.stages[kmeans_stage].computeCost(featured_dataset.sample(evaluation_fraction))

        print("Current cost " + str(current_result))
        out_file = open(save_results_folder + 'k_' + str(k), 'w')
        out_file.write(str(current_result))
        out_file.close()
        k_results[k] = current_result

    return k_results

appName = 'Parquet Converter'
master = 'local[7]'

sc = pyspark.SparkContext()
spark = SparkSession.builder.appName(appName).getOrCreate()

dataset_folder = '/home/bigdata/auxiliary/'
results_folder = '/home/bigdata/auxiliary/stats/'

#Whether to use the cleaned dataset or the uncleaned one
clean_dataset = True

#Build an entry for each archive to treat attaching the relative schema conversion routine to each one
archives = []
for year in range(2010, 2019):
    if year <= 2014:
        if year >= 2013:
            archives += ['green_tripdata_' + str(year)]
        archives += ['yellow_tripdata_' + str(year)]

    elif year <= 2016:
        archives += ['green_tripdata_' + str(year)]
        archives += ['yellow_tripdata_' + str(year)]

    else:
        archives += ['green_tripdata_' + str(year)]
        archives += ['yellow_tripdata_' + str(year)]

dataset = None

if not clean_dataset:
    #Open and convert each archive to parquet format
    for archive in archives:
        print("Reading: " + archive)

        current_dataset = spark.read.parquet('file://' + dataset_folder + archive + '_common.parquet')
        if dataset is None:
            dataset = current_dataset
        else:
            dataset = dataset.union(current_dataset)

else:
    dataset = spark.read.parquet('file://' + dataset_folder + 'clean_dataset.parquet')

def timestamp_diff(end_time, start_time):
    #Adds 1 to avoid divisions by 0
    return (end_time - start_time).seconds + 1

spark.udf.register("timestamp_diff", timestamp_diff, IntegerType())


#Obtained that the best k for the current dataset is k=5
#NOTE: comment line to skip the calculation
k_results = compute_k_elbow(dataset, spark, results_folder, k_from=7, k_to=20, step_size=1, training_fraction=0.025)

clustering_training_dataset = dataset.sample(0.03)

#Build the final clustering model
#NOTE: comment line to skip clustering if the clustering model is already present in the dataset folder
clustering_model = cluster(clustering_training_dataset, spark, max_clusters = 5, max_iterations = 40, clustering_prediction_property = clustering_class_property)

try:
    clustering_model.save('file://' + dataset_folder + 'clustering_model.model')
except:
    print("Clustering model already exists. Continuing without saving")


clustering_model = PipelineModel.load('file://' + dataset_folder + 'clustering_model.model')

#Assigns classes to the whole dataset
clustered_dataset = clustering_model.transform(dataset)

#Drops clustering columns
clustered_dataset = clustered_dataset.drop(taxi_company_indexed_property)
clustered_dataset = clustered_dataset.drop(pickup_hour_property)
clustered_dataset = clustered_dataset.drop(dropoff_hour_property)
clustered_dataset = clustered_dataset.drop(weekend_property)
clustered_dataset = clustered_dataset.drop(speed_property)
clustered_dataset = clustered_dataset.drop(taxi_company_encoded_property)
clustered_dataset = clustered_dataset.drop(ratecode_id_encoded_property)
clustered_dataset = clustered_dataset.drop(payment_type_encoded_property)
clustered_dataset = clustered_dataset.drop(unscaled_vector_property)
clustered_dataset = clustered_dataset.drop(scaled_vector_property)
clustered_dataset = clustered_dataset.drop(partial_clustering_features_property)
clustered_dataset = clustered_dataset.drop(clustering_features_property)

clustered_dataset.write.parquet('file://' + dataset_folder + 'clustered_dataset.parquet')
