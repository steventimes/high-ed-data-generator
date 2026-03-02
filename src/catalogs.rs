use rand::distributions::WeightedIndex;

pub static BRANDEIS_MAJORS: &[(&str, u32)] = &[
    ("Computer Science", 120),
    ("Biology", 110),
    ("Economics", 105),
    ("Psychology", 95),
    ("Neuroscience", 90),
    ("Biochemistry", 70),
    ("Health: Science, Society, and Policy", 55),
    ("Mathematics", 50),
    ("Physics", 45),
    ("Chemistry", 45),
    ("Politics", 60),
    ("International and Global Studies", 65),
    ("Sociology", 50),
    ("Anthropology", 40),
    ("American Studies", 40),
    ("English", 45),
    ("History", 45),
    ("Philosophy", 35),
    ("Studio Art", 30),
    ("Business", 35),
    ("Economics and Business", 30),
    ("Applied Mathematics", 30),
    ("Environmental Studies", 25),
    ("Music", 20),
    ("East Asian Studies", 15),
];

pub static BRANDEIS_SUBJECTS: &[&str] = &[
    "COSI", "BIOL", "ECON", "PSYC", "NEUR", "CHEM", "MATH", "HSSP", "AMST", "ENVS", "POL", "ANTH",
    "ENG", "HIST", "PHIL", "BUS",
];

pub fn build_major_sampler() -> (Vec<&'static str>, WeightedIndex<u32>) {
    let majors: Vec<&'static str> = BRANDEIS_MAJORS.iter().map(|(m, _)| *m).collect();
    let weights: Vec<u32> = BRANDEIS_MAJORS.iter().map(|(_, w)| *w).collect();
    let dist = WeightedIndex::new(weights).expect("major weights must be valid");
    (majors, dist)
}