from scripts.r0.base_models import CONDITION_TO_XRV, positives_from_findings


def test_certainty_rule_and_previous_status():
    f = [
        {"condition": "Tuberculosis", "certainty": "definite"},
        {"condition": "Silicosis", "certainty": "probable"},
        {"condition": "Pneumonia", "certainty": "possible"},
        {"condition": "Pleural Effusion", "certainty": "definite", "status": "previous"},
        {"condition": "Normal", "certainty": "definite"},
    ]
    assert positives_from_findings(f) == {"tuberculosis", "silicosis"}


def test_condition_map_targets_real_xrv_heads():
    heads = {"Atelectasis", "Consolidation", "Infiltration", "Pneumothorax", "Edema", "Emphysema", "Fibrosis",
             "Effusion", "Pneumonia", "Pleural_Thickening", "Cardiomegaly", "Nodule", "Mass", "Hernia",
             "Lung Lesion", "Fracture", "Lung Opacity", "Enlarged Cardiomediastinum"}
    assert set(CONDITION_TO_XRV.values()) <= heads
    assert "tuberculosis" not in CONDITION_TO_XRV
