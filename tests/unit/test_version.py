from deadline_tools.version import APP_VERSION, APP_VERSION_LABEL


def test_version_label_uses_central_project_version():
    assert APP_VERSION == "1.1.7"
    assert APP_VERSION_LABEL == "v.1.1.7"