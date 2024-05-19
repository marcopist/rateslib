use pyo3::pyfunction;
use pyo3::prelude::*;
use pyo3::types::PyList;
use crate::curves::interpolation::index_left;

// macro_rules! create_interface {
//     ($name: ident, $type: ident) => {
//
//         #[pyfunction]
//         pub fn $name (list_input: Vec<$type>, value: $type, left_count: Option<usize>) -> usize {
//             index_left(&list_input[..], &value, left_count)
//         }
//     };
// }
//
// create_interface!(index_left_f64, f64);

#[pyfunction]
pub fn index_left_f64(list_input: Bound<'_, PyList>, value: f64, left_count: Option<usize>) -> usize
{
    let lc = left_count.unwrap_or(0_usize);
    let n = list_input.len();
    match n {
        1 => panic!("`index_left` designed for intervals. Cannot index sequence of length 1."),
        2 => lc,
        _ => {
            let split = (n - 1_usize) / 2_usize;  // this will take the floor of result
            let lv = list_input.get_item(split).unwrap().extract::<f64>().unwrap();
            if n == 3 && (value-lv).abs() < 1e-15_f64 {
                lc
            } else if value <= lv {
                // println!("{}, {}", split, lc);
                index_left_f64(list_input.get_slice(0, split+1), value, Some(lc))
                // index_left(&list_input[..=split], value, Some(lc))
            } else {
                // println!("{}, {}", split, lc);
                index_left_f64(list_input.get_slice(split, n), value, Some(lc + split))
                // index_left(&list_input[split..], value, Some(lc + split))
            }
        }
    }
}
