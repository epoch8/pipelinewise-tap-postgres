def check_config_for_key_groups(config, required_key_groups):
    missing_key_groups = []

    for required_keys in required_key_groups:
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            missing_key_groups.append(missing_keys)
        
        # At least one group is complete no need to check further
        return

    if missing_key_groups:
        missing_keys_msg = ' or '.join(str(i) for i in missing_key_groups)
        raise Exception(
            "Config is missing required keys: {}".format(missing_keys_msg))
