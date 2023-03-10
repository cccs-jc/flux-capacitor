import sys
import time
import util
from util import (
    init_argparse,
    get_checkpoint_location,
    get_spark,
    create_spark_session,
    global_view,
    print_anomalies,
    validate_events,
    store_alerts
)


def find_parents(anomalies):
    anomalies.createOrReplaceGlobalTempView("anomalies")
    parent_anomalies = anomalies.where("detection_action = 'parent'")
    if parent_anomalies.count() == 0:
        return parent_anomalies
    parents = get_spark().sql(f"""
        select
            e.*,
            a.detection_id,
            a.detection_ts,
            a.detection_host,
            a.detection_rule_name,
            a.detection_action
        from
            global_temp.anomalies as a
            join {util.tagged_telemetry_table} as e 
            on a.detection_host = e.host_id
            and a.parent_id = e.id
        where
            a.detection_action = 'parent'
        """)
    return parent_anomalies.unionAll(parents)

def start_query(args):
    create_spark_session("streaming parent alert builder", 1)

    # current time in milliseconds
    ts = int(time.time() * 1000)

    anomalies = (
        get_spark()
        .readStream.format("iceberg")
        .option("stream-from-timestamp", ts)
        .option("streaming-skip-delete-snapshots", True)
        .load(util.suspected_anomalies_table)
    )

    global_view("sigma_rule_to_action")

    def foreach_batch_function(anomalies, epoch_id):
        # Transform and write batchDF
        anomalies.persist()
        parents = find_parents(anomalies)
        parents.persist()
        print_anomalies("context for historical parents:", parents)
        validated_parents = validate_events(parents)
        print_anomalies("validated historical parents:", validated_parents)
        store_alerts(validated_parents)
        parents.unpersist(True)
        anomalies.unpersist(True)
        get_spark().catalog.clearCache()
        anomalies.sparkSession.catalog.clearCache()

    streaming_query = (
        anomalies
        .writeStream
        .queryName("parents")
        .trigger(processingTime=f"{args.trigger} seconds")
        .option("checkpointLocation", get_checkpoint_location(util.alerts_table) + "_parents")
        .foreachBatch(foreach_batch_function)
        .start()
    )

    streaming_query.awaitTermination()





def main() -> int:
    args = init_argparse()
    start_query(args)
    return 0
    
if __name__ == "__main__":
    sys.exit(main())

