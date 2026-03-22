use rand::distributions::WeightedIndex;

#[derive(Clone, Copy)]
pub struct MajorCatalog {
    pub label: &'static str,
    pub cip_code: &'static str,
    pub college: &'static str,
    pub likely_subject: &'static str,
    pub weight: u32,
}

pub static MAJOR_CATALOG: &[MajorCatalog] = &[
    MajorCatalog {
        label: "Computer Science",
        cip_code: "11.0701",
        college: "Arts and Sciences",
        likely_subject: "COSI",
        weight: 120,
    },
    MajorCatalog {
        label: "Biology",
        cip_code: "26.0101",
        college: "Arts and Sciences",
        likely_subject: "BIOL",
        weight: 110,
    },
    MajorCatalog {
        label: "Economics",
        cip_code: "45.0601",
        college: "Business School",
        likely_subject: "ECON",
        weight: 105,
    },
    MajorCatalog {
        label: "Psychology",
        cip_code: "42.0101",
        college: "Arts and Sciences",
        likely_subject: "PSYC",
        weight: 95,
    },
    MajorCatalog {
        label: "Neuroscience",
        cip_code: "26.1501",
        college: "Arts and Sciences",
        likely_subject: "NEUR",
        weight: 90,
    },
    MajorCatalog {
        label: "Biochemistry",
        cip_code: "26.0202",
        college: "Arts and Sciences",
        likely_subject: "CHEM",
        weight: 70,
    },
    MajorCatalog {
        label: "Health Science, Society, and Policy",
        cip_code: "51.2208",
        college: "Arts and Sciences",
        likely_subject: "HSSP",
        weight: 55,
    },
    MajorCatalog {
        label: "Mathematics",
        cip_code: "27.0101",
        college: "Arts and Sciences",
        likely_subject: "MATH",
        weight: 50,
    },
    MajorCatalog {
        label: "Physics",
        cip_code: "40.0801",
        college: "Arts and Sciences",
        likely_subject: "PHYS",
        weight: 45,
    },
    MajorCatalog {
        label: "Chemistry",
        cip_code: "40.0501",
        college: "Arts and Sciences",
        likely_subject: "CHEM",
        weight: 45,
    },
    MajorCatalog {
        label: "Politics",
        cip_code: "45.1001",
        college: "Arts and Sciences",
        likely_subject: "POL",
        weight: 60,
    },
    MajorCatalog {
        label: "International and Global Studies",
        cip_code: "30.2001",
        college: "Arts and Sciences",
        likely_subject: "IGS",
        weight: 65,
    },
    MajorCatalog {
        label: "Sociology",
        cip_code: "45.1101",
        college: "Arts and Sciences",
        likely_subject: "SOC",
        weight: 50,
    },
    MajorCatalog {
        label: "Anthropology",
        cip_code: "45.0201",
        college: "Arts and Sciences",
        likely_subject: "ANTH",
        weight: 40,
    },
    MajorCatalog {
        label: "American Studies",
        cip_code: "05.0102",
        college: "Arts and Sciences",
        likely_subject: "AMST",
        weight: 40,
    },
    MajorCatalog {
        label: "English",
        cip_code: "23.0101",
        college: "Arts and Sciences",
        likely_subject: "ENG",
        weight: 45,
    },
    MajorCatalog {
        label: "History",
        cip_code: "54.0101",
        college: "Arts and Sciences",
        likely_subject: "HIST",
        weight: 45,
    },
    MajorCatalog {
        label: "Philosophy",
        cip_code: "38.0101",
        college: "Arts and Sciences",
        likely_subject: "PHIL",
        weight: 35,
    },
    MajorCatalog {
        label: "Studio Art",
        cip_code: "50.0702",
        college: "Arts and Sciences",
        likely_subject: "ARTS",
        weight: 30,
    },
    MajorCatalog {
        label: "Business",
        cip_code: "52.0201",
        college: "Business School",
        likely_subject: "BUS",
        weight: 35,
    },
    MajorCatalog {
        label: "Economics and Business",
        cip_code: "52.0101",
        college: "Business School",
        likely_subject: "BUS",
        weight: 30,
    },
    MajorCatalog {
        label: "Applied Mathematics",
        cip_code: "27.0301",
        college: "Arts and Sciences",
        likely_subject: "MATH",
        weight: 30,
    },
    MajorCatalog {
        label: "Environmental Studies",
        cip_code: "03.0103",
        college: "Arts and Sciences",
        likely_subject: "ENVS",
        weight: 25,
    },
    MajorCatalog {
        label: "Music",
        cip_code: "50.0901",
        college: "Arts and Sciences",
        likely_subject: "MUS",
        weight: 20,
    },
    MajorCatalog {
        label: "East Asian Studies",
        cip_code: "05.0103",
        college: "Arts and Sciences",
        likely_subject: "EAS",
        weight: 15,
    },
];

pub static SUBJECT_CATALOG: &[&str] = &[
    "COSI", "BIOL", "ECON", "PSYC", "NEUR", "CHEM", "MATH", "HSSP", "AMST", "ENVS", "POL",
    "ANTH", "ENG", "HIST", "PHIL", "BUS", "PHYS", "SOC", "IGS", "ARTS", "MUS", "EAS",
];

pub fn majors() -> &'static [MajorCatalog] {
    MAJOR_CATALOG
}

pub fn build_major_sampler() -> WeightedIndex<u32> {
    let weights: Vec<u32> = MAJOR_CATALOG.iter().map(|m| m.weight).collect();
    WeightedIndex::new(weights).expect("major weights must be valid")
}

pub fn cip_for_subject(subject: &str) -> &'static str {
    match subject {
        "COSI" => "11.0701",
        "BIOL" => "26.0101",
        "ECON" => "45.0601",
        "PSYC" => "42.0101",
        "NEUR" => "26.1501",
        "CHEM" => "40.0501",
        "MATH" => "27.0101",
        "HSSP" => "51.2208",
        "AMST" => "05.0102",
        "ENVS" => "03.0103",
        "POL" => "45.1001",
        "ANTH" => "45.0201",
        "ENG" => "23.0101",
        "HIST" => "54.0101",
        "PHIL" => "38.0101",
        "BUS" => "52.0201",
        "PHYS" => "40.0801",
        "SOC" => "45.1101",
        "IGS" => "30.2001",
        "ARTS" => "50.0702",
        "MUS" => "50.0901",
        "EAS" => "05.0103",
        _ => "24.0101",
    }
}
