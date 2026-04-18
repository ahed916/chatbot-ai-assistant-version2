# mlflow_config.py
import mlflow

MLFLOW_TRACKING_URI = "http://localhost:5000"
EXPERIMENT_NAME = "redmind-agents"


def setup_mlflow():
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT_NAME)
    try:
        mlflow.langchain.autolog(log_traces=True)
    except TypeError:
        # Older MLflow versions — use minimal autolog
        mlflow.langchain.autolog()


setup_mlflow()
