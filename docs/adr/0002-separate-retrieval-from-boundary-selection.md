# Separate retrieval from boundary selection

Candidate Moment retrieval identifies where interesting content may exist; boundary selection then examines a roughly two-to-five-minute Context Envelope and produces variable Clip Proposal start and end points. We reject fixed-duration clips because different proposal categories require different amounts of setup and resolution, accepting additional semantic-evaluation complexity in exchange for standalone coherence.
