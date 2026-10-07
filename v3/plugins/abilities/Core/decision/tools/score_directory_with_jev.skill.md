# score_directory_with_jev

Arguments: path, search_criteria, optional offset (default 0). Lists one
directory in the file_list path namespace and sends at most 32 entry names
and kinds to Jev. Returns each entry's score from 0 (irrelevant) to 4
(strong match), confidence, and next_offset for another page. File contents
are not inspected. The current session needs automatic file_list permission
as well as Decision permission. Verify promising candidates with file_read.
