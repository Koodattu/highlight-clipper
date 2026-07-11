UPDATE stage_attempts SET progress = 1 WHERE state = 'succeeded' AND progress < 1;
